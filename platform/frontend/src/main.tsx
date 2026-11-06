import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import "pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css";
import "./styles/index.css";

createRoot(document.getElementById("root")!).render(<App />);
