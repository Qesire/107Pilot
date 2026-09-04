import { RunsPage as CoreRunsPage } from "./pages.core";
import { RepairWorkspacePage } from "./RepairWorkspacePage";
import type { LocationState } from "./url";

export { WorkspacePage, ClusterPage, TerminalCollaborationPage } from "./pages.core";

interface PageProps {
  user: string;
  location: LocationState;
  navigate: (path: string) => void;
}

export function RunsPage({ user, location, navigate }: PageProps) {
  const selectedRunId = location.pathname.startsWith("/runs/")
    ? decodeURIComponent(location.pathname.slice("/runs/".length))
    : null;

  if (selectedRunId && location.search.get("tab") === "repair") {
    return (
      <RepairWorkspacePage
        user={user}
        location={location}
        navigate={navigate}
        runId={selectedRunId}
      />
    );
  }

  return <CoreRunsPage user={user} location={location} navigate={navigate} />;
}
