import { apiFetch } from "./client";

// GET /history and /stats/model both real-501 on the backend today (no settled outcomes /
// registered model metrics exist yet — see backend/app/history/router.py). Typed as unknown[]
// rather than left unimplemented here, so the history screen is ready the moment the backend
// is.
export function getHistory(): Promise<unknown[]> {
  return apiFetch<unknown[]>("/history");
}

export function getModelStats(): Promise<unknown[]> {
  return apiFetch<unknown[]>("/stats/model");
}
