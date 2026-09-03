import { useInfiniteQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "../api";
import type { FileEntry } from "../types";

export const FILE_DIRECTORY_PAGE_SIZE = 500;

export function fileDirectoryQueryKey(user: string, cwd: string) {
  return ["files-list", user, cwd] as const;
}

export function useFileDirectoryListing(
  user: string,
  cwd: string,
  pageSize = FILE_DIRECTORY_PAGE_SIZE,
) {
  const listing = useInfiniteQuery({
    queryKey: fileDirectoryQueryKey(user, cwd),
    queryFn: ({ signal, pageParam }) => api.fileList(
      user,
      cwd,
      { limit: pageSize, cursor: pageParam },
      signal,
    ),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.page.next_cursor ?? undefined,
    retry: false,
  });

  const entries = useMemo<FileEntry[]>(
    () => listing.data?.pages.flatMap((page) => page.entries) ?? [],
    [listing.data],
  );

  return {
    ...listing,
    entries,
    loadedCount: entries.length,
    error: listing.error as Error | null,
  };
}
