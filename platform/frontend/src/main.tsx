import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "./styles/index.css";

const root = createRoot(document.getElementById("root")!);
if (
  import.meta.env.DEV &&
  new URLSearchParams(window.location.search).get("connection-preview") === "1"
) {
  void import("./features/prepare/ConnectionPreviewApp").then(
    ({ ConnectionPreviewApp }) => root.render(<ConnectionPreviewApp />),
  );
} else {
  root.render(<App />);
}
