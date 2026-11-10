import { FormEvent, useEffect, useState } from "react";
import { api, Me } from "./api";
import { MatterList } from "./components/MatterList";
import { MatterView } from "./components/MatterView";

function useHashRoute(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

const ROLES = ["paralegal", "attorney", "admin", "auditor"];

function DevSignIn() {
  function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const user = String(f.get("user") || "").trim();
    const roles = ROLES.filter((r) => f.get(r)).join(",");
    document.cookie = `medchron_dev=${encodeURIComponent(user)}:${roles}; path=/; SameSite=Strict`;
    window.location.reload();
  }
  return (
    <div className="page-narrow">
      <section className="card">
        <h1>Sign in</h1>
        <p className="muted">
          Local development sign-in. Deployed environments use the firm's identity provider (OIDC), and this form is
          disabled there.
        </p>
        <form className="upload" onSubmit={onSubmit}>
          <label>
            User
            <input name="user" required pattern="[A-Za-z0-9_.@+\-]{1,128}" defaultValue="reviewer" />
          </label>
          <fieldset className="roles">
            {ROLES.map((r) => (
              <label key={r}>
                <input type="checkbox" name={r} defaultChecked={r === "attorney"} /> {r}
              </label>
            ))}
          </fieldset>
          <button type="submit">Sign in</button>
        </form>
      </section>
    </div>
  );
}

export function App() {
  const hash = useHashRoute();
  const match = hash.match(/^#\/matters\/([^/]+)/);
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null));
  }, []);
  const signOut = () => {
    document.cookie = "medchron_dev=; path=/; max-age=0";
    window.location.reload();
  };
  return (
    <div className="app">
      <header className="topbar">
        <a href="#/" className="brand">
          MedChron <span>reviewer</span>
        </a>
        <span className="banner" role="note">
          Synthetic data only · Draft output for attorney review · Not legal or medical advice
        </span>
        {me && (
          <span className="whoami" data-testid="whoami">
            {me.user_id} · {me.roles.join(", ") || "no roles"}{" "}
            <button onClick={signOut} className="small">
              Sign out
            </button>
          </span>
        )}
      </header>
      <main>
        {me === undefined ? null : me === null ? (
          <DevSignIn />
        ) : match ? (
          <MatterView matterId={decodeURIComponent(match[1])} me={me} />
        ) : (
          <MatterList />
        )}
      </main>
    </div>
  );
}
