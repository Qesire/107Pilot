FROM node:22.19.0-bookworm-slim@sha256:cff78eb5aa1cf27dc2b6aeea9d31366415a43e9a9ea0ddec00d780b2b66fad0f AS build

WORKDIR /opt/pilot-agentd
COPY services/pilot-agentd/package.json services/pilot-agentd/package-lock.json ./
RUN npm ci --ignore-scripts
COPY services/pilot-agentd/tsconfig.json ./
COPY services/pilot-agentd/src ./src
RUN npm run build && npm prune --omit=dev

FROM node:22.19.0-bookworm-slim@sha256:cff78eb5aa1cf27dc2b6aeea9d31366415a43e9a9ea0ddec00d780b2b66fad0f

ENV NODE_ENV=production
WORKDIR /opt/pilot-agentd
RUN groupadd --gid 10701 pilot-agentd \
    && useradd --uid 10701 --gid 10701 --no-create-home --shell /usr/sbin/nologin pilot-agentd
COPY --from=build /opt/pilot-agentd/package.json ./
COPY --from=build /opt/pilot-agentd/node_modules ./node_modules
COPY --from=build /opt/pilot-agentd/dist ./dist
USER 10701:10701
CMD ["node", "dist/main.js"]
