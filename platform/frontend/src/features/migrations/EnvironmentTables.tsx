import type { FleetEnvironment } from "../../api/migrations";
import { StatusDot } from "../../components/StatusDot";
import { appCell } from "./argo";
import { appRows } from "./fleetView";

export function AppMatrix({
  environments,
}: {
  environments: FleetEnvironment[];
}) {
  const names = appRows(environments);
  if (names.length === 0)
    return <p className="empty-line">수집된 앱이 없어요.</p>;
  return (
    <div className="table-wrap">
      <table className="dt">
        <thead>
          <tr>
            <th>앱</th>
            {environments.map((env) => (
              <th key={env.id}>{env.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map((name) => (
            <tr key={name}>
              <td className="strong">{name}</td>
              {environments.map((env) => {
                const cell = appCell(env.apps.find((a) => a.name === name));
                return (
                  <td key={env.id}>
                    <StatusDot tone={cell.tone}>{cell.label}</StatusDot>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
