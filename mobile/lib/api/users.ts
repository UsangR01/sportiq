import { apiFetch } from "./client";
import type { UserPreferencesResponse, UserPreferencesUpdate } from "./types";

export function getPreferences(): Promise<UserPreferencesResponse> {
  return apiFetch<UserPreferencesResponse>("/user/preferences");
}

export function updatePreferences(
  body: UserPreferencesUpdate
): Promise<UserPreferencesResponse> {
  return apiFetch<UserPreferencesResponse>("/user/preferences", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function updatePushToken(expoPushToken: string | null): Promise<void> {
  return apiFetch<void>("/user/push-token", {
    method: "PUT",
    body: JSON.stringify({ expo_push_token: expoPushToken }),
  });
}
