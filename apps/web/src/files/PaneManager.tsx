import { Fragment } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { collectPanes, type LayoutNode } from "./layout";
import { FilePane } from "./FilePane";
import type { PaneLayoutApi } from "./usePaneLayout";

function nodeKey(node: LayoutNode): string {
  return node.type === "pane" ? node.paneId : `g-${collectPanes(node).join(".")}`;
}

function RenderNode({
  node,
  homePath,
  layoutApi,
}: {
  node: LayoutNode;
  homePath: string;
  layoutApi: PaneLayoutApi;
}) {
  if (node.type === "pane") {
    const paneId = node.paneId;
    return (
      <FilePane
        paneId={paneId}
        homePath={homePath}
        onClose={() => layoutApi.close(paneId)}
        onSplitHorizontal={() => layoutApi.split(paneId, "horizontal")}
        onSplitVertical={() => layoutApi.split(paneId, "vertical")}
      />
    );
  }

  const size = Math.floor(100 / node.children.length);
  return (
    <Group orientation={node.direction} className="pane-group">
      {node.children.map((child, index) => (
        <Fragment key={nodeKey(child)}>
          {index > 0 && <Separator className="pane-resizer" />}
          <Panel defaultSize={`${size}`} minSize="16">
            <RenderNode node={child} homePath={homePath} layoutApi={layoutApi} />
          </Panel>
        </Fragment>
      ))}
    </Group>
  );
}

export function PaneManager({
  layoutApi,
  homePath,
}: {
  layoutApi: PaneLayoutApi;
  homePath: string;
}) {
  return (
    <div className="pane-manager">
      <RenderNode node={layoutApi.layout} homePath={homePath} layoutApi={layoutApi} />
    </div>
  );
}
