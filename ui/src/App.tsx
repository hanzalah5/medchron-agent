import { useEffect, useState } from "react";
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

export function App() {
  const hash = useHashRoute();
  const match = hash.match(/^#\/matters\/([^/]+)/);
  return (
    <div className="app">
      <header className="topbar">
        <a href="#/" className="brand">
          MedChron <span>reviewer</span>
        </a>
        <span className="banner" role="note">
          Synthetic data only · Draft output for attorney review · Not legal or medical advice
        </span>
      </header>
      <main>{match ? <MatterView matterId={decodeURIComponent(match[1])} /> : <MatterList />}</main>
    </div>
  );
}
