import { Platform } from "react-native";

import { clearTokens, getTokens, setTokens } from "@/lib/tokenStore";
import type { ApiErrorBody, TokenPair } from "./types";

function resolveBaseUrl(): string {
  const configured = process.env.EXPO_PUBLIC_API_URL ?? "http://localhost:8000";
  // The Android emulator's "localhost" is the emulator's own loopback, not the host
  // machine — 10.0.2.2 is the documented alias Android provides for the host. Real devices
  // need an actual LAN IP in EXPO_PUBLIC_API_URL instead (this rewrite only helps the
  // emulator case, since a real device has no such alias).
  if (Platform.OS === "android") {
    return configured.replace("localhost", "10.0.2.2").replace("127.0.0.1", "10.0.2.2");
  }
  return configured;
}

const BASE_URL = resolveBaseUrl();

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body: ApiErrorBody = await response.json();
    return body.detail ?? response.statusText;
  } catch {
    return response.statusText;
  }
}

/** No auth header, no 401-retry — used for the auth endpoints themselves, where attaching a
 * stale token or recursing into the refresh flow would make no sense. */
export async function rawFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

/** In-flight rotation, shared by every caller that needs one.
 *
 * WITHOUT THIS, OPENING THE APP LOGS YOU OUT. Refresh tokens rotate: /auth/refresh revokes
 * the presented token before issuing the next pair. The access token lasts 15 minutes, so
 * any open after that expires 401s EVERY authenticated request at once -- the feed alone
 * fires preferences and watchlist together -- and each one independently posted the same
 * stored refresh token. Measured against the real API: of two concurrent refreshes with one
 * token, the first returns 200 and the second returns 401. The loser then cleared the
 * session, which is why signing in was needed at practically every launch.
 *
 * One promise, so the second caller waits for the first rotation instead of racing it. */
let inFlightRefresh: Promise<boolean> | null = null;

function refreshTokens(): Promise<boolean> {
  inFlightRefresh ??= rotate().finally(() => {
    inFlightRefresh = null;
  });
  return inFlightRefresh;
}

async function rotate(): Promise<boolean> {
  const { refreshToken, email } = getTokens();
  if (!refreshToken) return false;
  try {
    const pair = await rawFetch<TokenPair>("/auth/refresh", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    await setTokens(pair.access_token, pair.refresh_token, email);
    return true;
  } catch (error) {
    // ONLY A REJECTED TOKEN ENDS THE SESSION. This used to clear on ANY failure, so a
    // request that never reached the server -- no signal, aeroplane mode, or the API cold
    // -- signed the user out on the strength of evidence that says nothing about whether
    // their token is still good. Keep it and let the next attempt decide; the request still
    // fails, which is honest, but the session survives.
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
      await clearTokens();
    }
    return false;
  }
}

/** The client every screen should use: attaches the access token, and on a single 401
 * transparently rotates the refresh token once (per TDD §4.3's rotating-refresh-token
 * design) before retrying — falling back to logging the user out if that also fails. */
export async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const withAuth = (token: string | null): RequestInit => ({
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });

  // Remembered, so the 401 below can tell "my token expired" from "someone else already
  // replaced it while this request was in flight" -- the latter needs a retry, not a rotation.
  const tokenUsed = getTokens().accessToken;
  let response = await fetch(`${BASE_URL}${path}`, withAuth(tokenUsed));

  if (response.status === 401 && tokenUsed) {
    const alreadyRotated = getTokens().accessToken !== tokenUsed;
    const refreshed = alreadyRotated || (await refreshTokens());
    if (refreshed) {
      response = await fetch(`${BASE_URL}${path}`, withAuth(getTokens().accessToken));
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, await parseErrorMessage(response));
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
