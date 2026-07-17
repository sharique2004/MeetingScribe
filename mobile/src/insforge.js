import { createClient } from "@insforge/sdk";

// Capture any OAuth error the provider bounced back with *before* the SDK
// strips it from the URL — createClient() runs its callback detection (which
// deletes insforge_code / error from the query string) synchronously below.
const _q = new URLSearchParams(window.location.search);
export const oauthReturnError = _q.get("insforge_error") || _q.get("error") || "";

export const insforge = createClient({
  baseUrl: import.meta.env.VITE_INSFORGE_URL,
  anonKey: import.meta.env.VITE_INSFORGE_ANON_KEY,
});
