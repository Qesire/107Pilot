import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { configFromEnv, type AgentdConfig } from "./config.js";
import {
  createCampusModelRuntime,
  createFauxModelRuntime,
  type ModelRuntime,
} from "./models.js";
import { closeAgentdServer, createAgentdServer } from "./server.js";
import { TurnExecutor } from "./turn-executor.js";

export function createAgentdExecutor(config: AgentdConfig): TurnExecutor {
  let runtime: ModelRuntime | undefined;
  if (config.configured) {
    runtime =
      config.modelProfile.provider === "faux"
        ? createFauxModelRuntime(
            config.modelProfile.fauxScenario === undefined
              ? {}
              : { scenario: config.modelProfile.fauxScenario },
          )
        : createCampusModelRuntime(config.modelProfile);
  }
  return new TurnExecutor((profileId) =>
    runtime?.profile.id === profileId ? runtime : undefined,
  );
}

export function createAgentdApplication(env: NodeJS.ProcessEnv = process.env) {
  const config = configFromEnv(env);
  const executor = createAgentdExecutor(config);
  const server = createAgentdServer(config, executor);
  return { config, executor, server };
}

export async function runAgentd(env: NodeJS.ProcessEnv = process.env): Promise<void> {
  const { config, server } = createAgentdApplication(env);
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once("error", rejectListen);
    server.listen(config.port, config.host, () => {
      server.off("error", rejectListen);
      resolveListen();
    });
  });

  let shutdown: Promise<void> | undefined;
  const stop = (): void => {
    shutdown ??= closeAgentdServer(server).then(() => {
      process.exitCode = 0;
    });
    void shutdown.catch(() => {
      process.exitCode = 1;
    });
  };
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
}

function isMainModule(): boolean {
  const entry = process.argv[1];
  if (entry === undefined) return false;
  return import.meta.url === pathToFileURL(resolve(entry)).href;
}

if (isMainModule()) {
  void runAgentd().catch(() => {
    process.exitCode = 1;
  });
}
