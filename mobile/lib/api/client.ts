import { clearTokens, getTokens, setTokens } from "@/lib/tokenStore";
import type { ApiErrorBody, TokenPair } from "./types";

const BASE_URL = process.env.EXPO_PUBLIC_API_URL ?? "http://localhost:8000";

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

async function refreshTokens(): Promise<boolean> {
  const { refreshToken, email } = getTokens();
  if (!refreshToken) return false;
  try {
    const pair = await rawFetch<TokenPair>("/auth/refresh", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    await setTokens(pair.access_token, pair.refresh_token, email);
    return true;
  } catch {
    await clearTokens();
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

  let response = await fetch(`${BASE_URL}${path}`, withAuth(getTokens().accessToken));

  if (response.status === 401 && getTokens().accessToken) {
    const refreshed = await refreshTokens();
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
