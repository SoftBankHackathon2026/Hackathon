import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { getAccess, logIn, logOut, signUp } from "../../api/auth";
import type { Access, Account } from "../../api/types";

const ACCESS_RETRY_MS = 4000;

export function accessQueryOptions() {
  return queryOptions({
    queryKey: ["access"] as const,
    queryFn: ({ signal }) => getAccess(signal),
    refetchInterval: (query) =>
      query.state.status === "error" ? ACCESS_RETRY_MS : false,
  });
}

export function useSetUser() {
  const queryClient = useQueryClient();
  return (user: Account | null) =>
    queryClient.setQueryData<Access>(
      accessQueryOptions().queryKey,
      (old) => old && { ...old, user },
    );
}

export function useAuthenticate(signup: boolean) {
  return useMutation({
    mutationFn: ({
      email,
      password,
      team,
    }: {
      email: string;
      password: string;
      team: string;
    }) => (signup ? signUp(email, password, team) : logIn(email, password)),
  });
}

export function useSignOut() {
  const setUser = useSetUser();
  return useMutation({
    mutationFn: () => logOut().catch(() => undefined),
    onSettled: () => setUser(null),
  });
}
