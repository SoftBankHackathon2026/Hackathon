import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { installStaleChunkReload } from "./app/staleChunk";
import "pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css";
import "./styles/index.css";

installStaleChunkReload(window);

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
