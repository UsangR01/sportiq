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
