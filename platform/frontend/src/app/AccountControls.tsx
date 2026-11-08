import type { Access } from "../api/types";
import { roleLabel } from "../features/team/roles";

export function AccountControls({
  access,
  onSignOut,
}: {
  access: Access;
  onSignOut: () => void;
}) {
  if (access.user)
    return (
      <details className="account-menu">
        <summary aria-label="계정">
          <span className="avatar" aria-hidden="true">
            {access.user.email.slice(0, 1).toUpperCase()}
          </span>
          <span className="account-team">{access.user.team}</span>
        </summary>
        <div className="menu">
          <strong>{access.user.email}</strong>
          <span className="menu-row">
            {access.user.team} 팀
            <span className={`role role-${access.user.role}`}>
              {roleLabel[access.user.role]}
            </span>
          </span>
          <button className="secondary" onClick={onSignOut}>
            로그아웃
          </button>
        </div>
      </details>
    );
  return null;
}
