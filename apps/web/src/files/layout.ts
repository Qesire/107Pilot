// Pure layout-tree model for the QSpace-style multi-pane file manager.
//
// A layout is a binary-ish tree: leaves are panes, internal nodes are
// resizable groups with a direction. All operations are pure so they can be
// unit-tested without React; the usePaneLayout hook wraps them with state and
// localStorage persistence.

export type SplitDirection = "horizontal" | "vertical";

export type LayoutNode =
  | { type: "pane"; paneId: string }
  | { type: "group"; direction: SplitDirection; children: LayoutNode[] };

let counter = 0;

/** Generate a process-unique pane id. */
export function newPaneId(): string {
  counter += 1;
  return `pane-${Date.now().toString(36)}-${counter.toString(36)}`;
}

/** The initial layout: a single full-width pane (the Miller-column view
 * wants the width; users can still split panes on demand). */
export function defaultLayout(): LayoutNode {
  return { type: "pane", paneId: newPaneId() };
}

/** Collect every pane id in tree order (left-to-right / top-to-bottom). */
export function collectPanes(node: LayoutNode): string[] {
  if (node.type === "pane") return [node.paneId];
  return node.children.flatMap(collectPanes);
}

/** True when the tree contains the given pane id. */
export function hasPane(node: LayoutNode, paneId: string): boolean {
  return collectPanes(node).includes(paneId);
}

// Replace the pane with id `paneId` by wrapping it in a new group that holds
// the original pane plus a freshly created sibling. Returns the new tree and
// the new pane id, or null when the target pane is not present.
function splitAt(
  node: LayoutNode,
  paneId: string,
  direction: SplitDirection,
  freshId: string,
): LayoutNode | null {
  if (node.type === "pane") {
    if (node.paneId !== paneId) return null;
    return {
      type: "group",
      direction,
      children: [
        { type: "pane", paneId },
        { type: "pane", paneId: freshId },
      ],
    };
  }
  let changed = false;
  const children = node.children.map((child) => {
    const next = splitAt(child, paneId, direction, freshId);
    if (next) changed = true;
    return next ?? child;
  });
  if (!changed) return null;
  return { type: "group", direction: node.direction, children };
}

/**
 * Split `paneId` into two panes along `direction`. Returns the new tree and the
 * id of the newly created pane. Throws if the pane is missing (caller bug).
 */
export function splitPane(
  root: LayoutNode,
  paneId: string,
  direction: SplitDirection,
): { root: LayoutNode; newPaneId: string } {
  const freshId = newPaneId();
  const next = splitAt(root, paneId, direction, freshId);
  if (!next) throw new Error(`splitPane: pane not found: ${paneId}`);
  return { root: next, newPaneId: freshId };
}

// Remove a pane and collapse any group that degenerates to a single child.
// Returns null when the subtree becomes empty.
function removeAt(node: LayoutNode, paneId: string): LayoutNode | null {
  if (node.type === "pane") {
    return node.paneId === paneId ? null : node;
  }
  const children: LayoutNode[] = [];
  for (const child of node.children) {
    const next = removeAt(child, paneId);
    if (next) children.push(next);
  }
  if (children.length === 0) return null;
  if (children.length === 1) return children[0] ?? null;
  return { type: "group", direction: node.direction, children };
}

/**
 * Close `paneId`. The last remaining pane cannot be closed (the tree is
 * returned unchanged) so the manager always shows at least one pane.
 */
export function closePane(root: LayoutNode, paneId: string): LayoutNode {
  if (collectPanes(root).length <= 1) return root;
  const next = removeAt(root, paneId);
  return next ?? root;
}

/**
 * Validate a value loaded from storage. Accepts only well-formed trees with at
 * least one pane; otherwise returns null so the caller falls back to default.
 */
export function parseLayout(value: unknown): LayoutNode | null {
  if (!value || typeof value !== "object") return null;
  const node = value as LayoutNode;
  if (node.type === "pane") {
    return typeof node.paneId === "string" && node.paneId ? node : null;
  }
  if (node.type !== "group") return null;
  if (node.direction !== "horizontal" && node.direction !== "vertical") return null;
  if (!Array.isArray(node.children)) return null;
  const children = node.children.map(parseLayout);
  if (children.some((child) => child === null)) return null;
  const valid = children as LayoutNode[];
  if (valid.length < 1) return null;
  if (valid.length === 1) return valid[0] ?? null;
  return { type: "group", direction: node.direction, children: valid };
}
