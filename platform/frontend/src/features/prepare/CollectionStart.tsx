import { Icon } from "../../components/Icon";
import type { ConnectionSnapshot, Provider } from "./connectionTypes";
import { remainingSeconds } from "./model";
export function CollectionStart({
  snapshot: s,
  available,
  preview,
  now,
  busy,
  acknowledged,
  onAcknowledge,
  onCopy,
  provider,
  providers,
  onProvider,
}: {
  snapshot: ConnectionSnapshot | null;
  available: boolean;
  preview: boolean;
  now: number;
  busy: boolean;
  acknowledged: boolean;
  onAcknowledge: (value: boolean) => void;
  onCopy: () => void;
  provider: Provider;
  providers: readonly Provider[];
  onProvider: (provider: Provider) => void;
}) {
  return (
    <section>
      <fieldset className="connection-provider">
        <legend>환경 유형</legend>
        <div>
          {(
            [
              ["aws", "AWS"],
              ["gcp", "GCP"],
              ["kubernetes", "온프레미스"],
            ] as const
          ).map(([value, label]) => (
            <label key={value}>
              <input
                type="radio"
                name="connection-provider"
                value={value}
                checked={provider === value}
                disabled={busy || Boolean(s) || !providers.includes(value)}
                onChange={() => onProvider(value)}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
        {s && (
          <p className="connection-note">
            환경 유형을 바꾸려면 새 환경정보 수집을 시작하세요.
          </p>
        )}
        {!s && available && providers.length === 1 && (
          <p className="connection-note">
            이 조직에서 AWS·GCP 연결을 아직 사용할 수 없어요.
          </p>
        )}
      </fieldset>
      <header className="connection-title">
        <h2>
          {s?.registration === "EXPIRED"
            ? "명령 기한이 끝났어요"
            : "먼저 환경정보를 수집할게요"}
        </h2>
        <p>
          인프라에 접근할 수 있는 단말에서 명령 한 줄을 실행하세요. 단말 확인과
          보안 연결은 이 단계에서 이어져요.
        </p>
      </header>
      <div className="connection-readiness">
        <h3>실행할 단말 준비</h3>
        <dl>
          <div>
            <dt>운영체제</dt>
            <dd>Ubuntu 24.04 LTS, amd64 또는 arm64</dd>
          </div>
          <div>
            <dt>도구</dt>
            <dd>
              Bash, curl, Python 3, kubectl
              {provider === "aws" && ", AWS 명령줄 도구(aws)"}
              {provider === "gcp" &&
                ", Google Cloud 명령줄 도구(gcloud), GKE 인증 도구"}
            </dd>
          </div>
          <div>
            <dt>관리 권한</dt>
            <dd>
              사전 설치된 네트워크 보조 프로그램과 해당 프로그램의 실행 권한
            </dd>
          </div>
          <div>
            <dt>외부 통신</dt>
            <dd>TCP 443, 8443, UDP 51820</dd>
          </div>
        </dl>
        <p>실제 도구, 권한, 시계, 통신 상태는 단말에서 확인해요.</p>
      </div>
      <label className="connection-check">
        <input
          type="checkbox"
          checked={acknowledged}
          disabled={busy}
          onChange={(e) => onAcknowledge(e.target.checked)}
        />
        <span>이 환경에 접근할 수 있는 지원 단말을 준비했어요.</span>
      </label>
      <div className="connection-command">
        <div>
          <h3>환경정보 수집 명령</h3>
          <span>등록 비밀은 화면에 표시하지 않아요.</span>
        </div>
        <pre aria-label="등록 명령 형식">
          {preview
            ? "# 미리보기용 안내만 복사돼요. 실제 실행 명령이 없어요."
            : "curl -qfsS --proto '=https' <배포 주소>/i | MIGRATION_ENROLL='<요청 ID>.••••••••' bash"}
        </pre>
      </div>
      <div className="connection-actions">
        <button
          className="primary"
          disabled={!available || !acknowledged || busy}
          onClick={onCopy}
        >
          <Icon name="code" size={16} />
          {busy
            ? "복사 준비 중…"
            : s
              ? "새 명령 복사"
              : preview
                ? "미리보기 명령 복사"
                : "명령 복사"}
        </button>
        {s && (
          <span role="timer" aria-label="등록까지 남은 시간">
            단말 등록 접수까지{" "}
            <strong>{remainingSeconds(s.registrationExpiresAt, now)}초</strong>
          </span>
        )}
      </div>
      <p className="connection-note">
        60초는 단말 등록 증명의 접수 기한이에요. 접수 후 웹 확인은 최대 5분 안에
        마치면 돼요. 환경 이름은 수집 후 입력해요.
      </p>
    </section>
  );
}
