import { describe, expect, it } from "vitest";

import {
  AGENTD_VERSION,
  EVENT_PROTOCOL_VERSION,
  TURN_PROTOCOL_VERSION,
} from "../src/version.js";

describe("pilot-agentd runtime baseline", () => {
  it("supports the declared Node 22 runtime range", () => {
    const [major, minor] = process.versions.node.split(".").map(Number);

    expect(major).toBe(22);
    expect(minor).toBeGreaterThanOrEqual(19);
  });

  it("pins the A0 service and wire versions", () => {
    expect(AGENTD_VERSION).toBe("0.1.0");
    expect(TURN_PROTOCOL_VERSION).toBe("pilot107.agent-turn-request/v1");
    expect(EVENT_PROTOCOL_VERSION).toBe("pilot107.agent-turn-event/v1");
  });
});
