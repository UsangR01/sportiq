import { deleteItem, getItem, setItem } from "@/lib/storage";

// Plain module (no Zustand, no React) so it can be imported by both the API client (which
// needs to read/rotate tokens without knowing about React) and store/authStore.ts (which
// wraps this for UI reactivity) without the two importing each other — that circular
// import previously showed up as Metro's "Require cycle" warning at bundle time.

const ACCESS_TOKEN_KEY = "sportiq_access_token";
const REFRESH_TOKEN_KEY = "sportiq_refresh_token";
const USER_EMAIL_KEY = "sportiq_user_email";

export interface Tokens {
  accessToken: string | null;
  refreshToken: string | null;
  email: string | null;
}

let cache: Tokens = { accessToken: null, refreshToken: null, email: null };
type Listener = (tokens: Tokens) => void;
const listeners = new Set<Listener>();

function notify() {
  listeners.forEach((listener) => listener(cache));
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function getTokens(): Tokens {
  return cache;
}

export async function hydrateTokens(): Promise<Tokens> {
  const [accessToken, refreshToken, email] = await Promise.all([
    getItem(ACCESS_TOKEN_KEY),
    getItem(REFRESH_TOKEN_KEY),
    getItem(USER_EMAIL_KEY),
  ]);
  cache = { accessToken, refreshToken, email };
  notify();
  return cache;
}

export async function setTokens(
  accessToken: string,
  refreshToken: string,
  email?: string | null
): Promise<Tokens> {
  const resolvedEmail = email ?? cache.email;
  await Promise.all([
    setItem(ACCESS_TOKEN_KEY, accessToken),
    setItem(REFRESH_TOKEN_KEY, refreshToken),
    resolvedEmail ? setItem(USER_EMAIL_KEY, resolvedEmail) : Promise.resolve(),
  ]);
  cache = { accessToken, refreshToken, email: resolvedEmail };
  notify();
  return cache;
}

export async function clearTokens(): Promise<void> {
  await Promise.all([
    deleteItem(ACCESS_TOKEN_KEY),
    deleteItem(REFRESH_TOKEN_KEY),
    deleteItem(USER_EMAIL_KEY),
  ]);
  cache = { accessToken: null, refreshToken: null, email: null };
  notify();
}
