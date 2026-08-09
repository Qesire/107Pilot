import { describe, expect, it } from "vitest";
import {
  closePane,
  collectPanes,
  defaultLayout,
  hasPane,
  parseLayout,
  splitPane,
  type LayoutNode,
} from "./layout";

const pane = (paneId: string): LayoutNode => ({ type: "pane", paneId });
const group = (
  direction: "horizontal" | "vertical",
  children: LayoutNode[],
): LayoutNode => ({ type: "group", direction, children });

describe("defaultLayout", () => {
  it("starts with a single full-width pane", () => {
    const layout = defaultLayout();
    const panes = collectPanes(layout);
    expect(panes).toHaveLength(1);
    expect(layout.type).toBe("pane");
  });
});

describe("splitPane", () => {
  it("wraps the target pane in a group with a new sibling", () => {
    const root = group("horizontal", [pane("a"), pane("b")]);
    const { root: next, newPaneId } = splitPane(root, "a", "vertical");
    const panes = collectPanes(next);
    expect(panes).toContain("a");
    expect(panes).toContain("b");
    expect(panes).toContain(newPaneId);
    expect(panes).toHaveLength(3);
    // The split node is now a vertical group holding "a" + the new pane.
    expect(next).toEqual(
      group("horizontal", [
        group("vertical", [pane("a"), pane(newPaneId)]),
        pane("b"),
      ]),
    );
  });

  it("throws when the pane is missing", () => {
    const root = group("horizontal", [pane("a")]);
    expect(() => splitPane(root, "nope", "vertical")).toThrow();
  });
});

describe("closePane", () => {
  it("removes a pane and collapses the degenerate group", () => {
    const root = group("horizontal", [
      group("vertical", [pane("a"), pane("b")]),
      pane("c"),
    ]);
    const next = closePane(root, "b");
    // The vertical group degenerates to a single pane "a".
    expect(next).toEqual(group("horizontal", [pane("a"), pane("c")]));
  });

  it("refuses to close the last pane", () => {
    const root = pane("only");
    expect(closePane(root, "only")).toEqual(root);
  });

  it("collapses nested groups down to a bare pane", () => {
    const root = group("horizontal", [
      group("vertical", [pane("a"), pane("b")]),
    ]);
    const next = closePane(root, "a");
    expect(next).toEqual(pane("b"));
  });
});

describe("hasPane", () => {
  it("detects presence in a nested tree", () => {
    const root = group("horizontal", [group("vertical", [pane("x")]), pane("y")]);
    expect(hasPane(root, "x")).toBe(true);
    expect(hasPane(root, "z")).toBe(false);
  });
});

describe("parseLayout", () => {
  it("round-trips a serialized layout", () => {
    const root = group("horizontal", [pane("a"), pane("b")]);
    const parsed = parseLayout(JSON.parse(JSON.stringify(root)));
    expect(parsed).toEqual(root);
  });

  it("rejects malformed input", () => {
    expect(parseLayout(null)).toBeNull();
    expect(parseLayout({})).toBeNull();
    expect(parseLayout({ type: "group", direction: "diagonal", children: [] })).toBeNull();
    expect(parseLayout({ type: "pane" })).toBeNull();
    expect(parseLayout({ type: "group", direction: "horizontal", children: [] })).toBeNull();
  });

  it("collapses single-child groups on parse", () => {
    const parsed = parseLayout({
      type: "group",
      direction: "horizontal",
      children: [{ type: "pane", paneId: "solo" }],
    });
    expect(parsed).toEqual(pane("solo"));
  });
});
