import { useMutation, useQueryClient } from "@tanstack/react-query";
import { deleteMigration } from "../../api/taskPlan";

export function useDeleteTask(jobId: string, onDeleted?: () => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => deleteMigration(jobId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["fleet"] });
      onDeleted?.();
    },
  });
}

export function confirmDeleteTask(jobId: string) {
  return window.confirm(`${jobId} 작업을 지울까요? 되돌릴 수 없어요.`);
}
