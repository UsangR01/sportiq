import { rawFetch } from "./client";
import type { GuestSessionResponse, GuestSessionState, TokenPair } from "./types";

export function register(
  email: string,
  password: string,
  guestSessionId?: string | null
): Promise<TokenPair> {
  return rawFetch<TokenPair>("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, guest_session_id: guestSessionId ?? null }),
  });
}

export function login(email: string, password: string): Promise<TokenPair> {
  return rawFetch<TokenPair>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function refresh(refreshToken: string): Promise<TokenPair> {
  return rawFetch<TokenPair>("/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
}

export function createGuestSession(): Promise<GuestSessionResponse> {
  return rawFetch<GuestSessionResponse>("/guest/session", { method: "POST" });
}

export function updateGuestSession(
  guestSessionId: string,
  state: GuestSessionState
): Promise<void> {
  return rawFetch<void>(`/guest/session/${guestSessionId}`, {
    method: "PUT",
    body: JSON.stringify(state),
  });
}
