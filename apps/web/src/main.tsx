import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./styles.css";
import "./design-system.css";
import "./styles/tokens.css";
import "./styles/themes.css";
import "./styles/foundation.css";
import "./styles/shell-v2.css";
import "./styles/workbench-v2.css";
import "./styles/workbench-menu-v2.css";
import "./styles/file-workspace-v2.css";
import "./styles/task-indicator-v2.css";
import "./styles/file-picker-v2.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      retry: (failureCount, error) => {
        const status = typeof error === "object" && error && "status" in error
          ? Number(error.status)
          : 0;
        return failureCount < 1 && status >= 500;
      },
      refetchOnWindowFocus: true,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
