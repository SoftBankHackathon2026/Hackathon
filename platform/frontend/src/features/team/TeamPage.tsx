import { useSuspenseQuery } from "@tanstack/react-query";
import { useState } from "react";
import { errorMessage } from "../../api/client";
import type { Account, Role } from "../../api/types";
import { PageHeader, Section } from "../../components/PageKit";
import { roleHelp, roleLabel } from "./roles";
import { teamQueryOptions, useSetRole } from "./queries";

export function TeamPage({ account }: { account: Account }) {
  const { data } = useSuspenseQuery(teamQueryOptions());
  const setRole = useSetRole();
  const [error, setError] = useState("");
  const owner = account.role === "owner";
  const saving = setRole.isPending ? setRole.variables.email : "";
  async function change(email: string, role: Role) {
    setError("");
    try {
      await setRole.mutateAsync({ email, role });
    } catch (e) {
      setError(errorMessage(e, "권한을 바꾸지 못했어요."));
    }
  }
  return (
    <div className="pk-page">
      <PageHeader title="팀" />
      <p className="pk-empty">
        {owner
          ? "새로 가입한 팀원은 보기 전용으로 시작해요. 권한은 관리자가 바꿀 수 있어요."
          : "권한을 바꾸려면 팀 관리자에게 요청하세요."}
      </p>
      {error && (
        <p className="banner" role="alert">
          {error}
        </p>
      )}
      <Section title="팀원" meta={data.members.length}>
        <div
          className="table-wrap"
          tabIndex={0}
          role="region"
          aria-label="팀원 목록"
        >
          <table className="pk-table">
            <thead>
              <tr>
                <th>이메일</th>
                <th>권한</th>
                <th>할 수 있는 일</th>
                <th>가입</th>
              </tr>
            </thead>
            <tbody>
              {data.members.map((m) => (
                <tr key={m.email}>
                  <td>
                    {m.email}
                    {m.email === account.email && <em> (나)</em>}
                  </td>
                  <td>
                    {owner ? (
                      <select
                        aria-label={`${m.email} 권한`}
                        value={m.role}
                        disabled={saving === m.email}
                        onChange={(e) =>
                          void change(m.email, e.target.value as Role)
                        }
                      >
                        {(Object.keys(roleLabel) as Role[]).map((role) => (
                          <option key={role} value={role}>
                            {roleLabel[role]}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className={`role role-${m.role}`}>
                        {roleLabel[m.role]}
                      </span>
                    )}
                  </td>
                  <td className="sub">{roleHelp[m.role]}</td>
                  <td className="sub">
                    {new Date(m.created_at).toLocaleDateString("ko-KR")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
