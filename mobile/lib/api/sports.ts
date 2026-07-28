import { apiFetch } from "./client";
import type { SportResponse } from "./types";

export function listSports(): Promise<SportResponse[]> {
  return apiFetch<SportResponse[]>("/sports");
}
