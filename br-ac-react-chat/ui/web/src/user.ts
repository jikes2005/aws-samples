// The lab version has no sign-in: the user types a name, kept in the URL (?user=) and in
// localStorage so a refresh or bookmark keeps it.

const KEY = "br-ac-react-chat:user";

export function savedUser(): string {
  const fromUrl = new URLSearchParams(window.location.search).get("user")?.trim();
  if (fromUrl) return fromUrl;
  try {
    return localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveUser(name: string): void {
  try {
    if (name) localStorage.setItem(KEY, name);
    else localStorage.removeItem(KEY);
  } catch {
    /* storage unavailable - the URL still carries the name */
  }
  const url = new URL(window.location.href);
  if (name) url.searchParams.set("user", name);
  else url.searchParams.delete("user");
  window.history.replaceState(null, "", url);
}
