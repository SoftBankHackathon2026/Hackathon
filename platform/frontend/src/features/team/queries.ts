import {
  queryOptions,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
import { getTeam, setRole } from "../../api/team";
import type { Member, Role } from "../../api/types";

type TeamData = Awaited<ReturnType<typeof getTeam>>;

export function teamQueryOptions() {
  return queryOptions({
    queryKey: ["team"] as const,
    queryFn: ({ signal }) => getTeam(signal),
  });
}

export function useSetRole() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ email, role }: { email: string; role: Role }) =>
      setRole(email, role),
    onSuccess: (updated: Member, { email }) =>
      queryClient.setQueryData<TeamData>(
        teamQueryOptions().queryKey,
        (old) =>
          old && {
            ...old,
            members: old.members.map((m) => (m.email === email ? updated : m)),
          },
      ),
  });
}
