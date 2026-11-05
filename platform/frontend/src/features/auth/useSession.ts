import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { UNAUTHORIZED_EVENT } from "../../api/client";
import type { Account } from "../../api/types";
import { accessQueryOptions, useSetUser, useSignOut } from "./queries";

export function useSession() {
  const queryClient = useQueryClient();
  const query = useQuery(accessQueryOptions());
  const setUser = useSetUser();
  const signOut = useSignOut();
  useEffect(() => {
    const rejected = () =>
      void queryClient.invalidateQueries({
        queryKey: accessQueryOptions().queryKey,
      });
    window.addEventListener(UNAUTHORIZED_EVENT, rejected);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, rejected);
  }, [queryClient]);
  return {
    access: query.data ?? null,
    connecting: query.isError,
    signedIn: (user: Account) => setUser(user),
    signOut: () => signOut.mutateAsync(),
  };
}
