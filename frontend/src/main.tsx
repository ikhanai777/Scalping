import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import MobileApp from "./MobileApp";
import "./styles.css";

const QUERY = "(max-width: 820px)";

function Root() {
  // The Android app always uses the phone layout; browsers switch by width.
  const forced = !!window.AndroidBridge || new URLSearchParams(location.search).has("mobile");
  const [mobile, setMobile] = useState(() => forced || window.matchMedia(QUERY).matches);
  useEffect(() => {
    if (forced) return;
    const mq = window.matchMedia(QUERY);
    const on = () => setMobile(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [forced]);
  useEffect(() => { document.body.classList.toggle("mobile", mobile); }, [mobile]);
  return mobile ? <MobileApp /> : <App />;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
