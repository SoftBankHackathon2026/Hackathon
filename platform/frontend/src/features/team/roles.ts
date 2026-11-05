import type { Role } from "../../api/types";

export const roleLabel: Record<Role, string> = {
  owner: "관리자",
  member: "팀원",
  viewer: "보기 전용",
};

export const roleHelp: Record<Role, string> = {
  owner: "현황을 보고, 팀원의 권한을 바꿀 수 있어요.",
  member: "마이그레이션 현황과 보안 상태를 볼 수 있어요.",
  viewer: "현황을 볼 수만 있어요. 권한은 관리자가 바꿔요.",
};
