interface ReleaseIdentity {
  template_id: string;
  release_version: string;
}

export function detailVersions(
  releases: readonly ReleaseIdentity[],
  templateId: string,
  requestedVersion: string | null,
): string[] {
  const versions = releases
    .filter((release) => release.template_id === templateId)
    .map((release) => release.release_version);
  if (requestedVersion) versions.push(requestedVersion);
  return [...new Set(versions)].sort(compareVersions).reverse();
}

function compareVersions(left: string, right: string): number {
  return left.localeCompare(right, undefined, { numeric: true, sensitivity: "base" });
}
