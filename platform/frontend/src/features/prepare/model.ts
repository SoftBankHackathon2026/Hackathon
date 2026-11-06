// 마이그레이션 준비 — 1단계 환경 연결.
// 사용자는 이름과 접속 주소 정도만 입력하고, 나머지(노드·CPU·메모리·버전 등)는 사용자가 셸에서 실행하는
// 등록 스크립트가 읽어 온다. 지금 화면은 데모라서 등록 ID·토큰을 브라우저에서 만든다.

export type EnvKind = "kubernetes" | "aws" | "gcp";
export type EnvRole = "source" | "target";
export type AwsScope = "viewonly" | "readonly";

export type SshAuth = "key" | "password";
export type KubeconfigSource = "path" | "paste";

// 온프렘은 플랫폼이 접속지(kubectl 이 되는 호스트)에 SSH 로 들어가 셸을 쥐고 직접 읽는다.
export type KubernetesInput = {
  name: string;
  role: EnvRole;
  host: string;
  port: string;
  user: string;
  auth: SshAuth;
  privateKey: string;
  password: string;
  kubeconfigSource: KubeconfigSource;
  kubeconfigPath: string;
  kubeconfigText: string;
  context: string;
};

export type AwsInput = {
  name: string;
  role: EnvRole;
  region: string;
  scope: AwsScope;
  roleName: string;
  boundary: string;
};

export type Ticket = {
  id: string;
  token: string;
  externalId: string;
  expiresAt: Date;
};

export type CommandStep = { title: string; comment: string; lines: string[] };

export const SSH_AUTH_OPTIONS: { value: SshAuth; label: string }[] = [
  { value: "key", label: "SSH 키" },
  { value: "password", label: "비밀번호" },
];

export const KUBECONFIG_OPTIONS: { value: KubeconfigSource; label: string }[] =
  [
    { value: "path", label: "접속지의 파일 경로" },
    { value: "paste", label: "내용 붙여넣기" },
  ];

export function kubernetesReady(input: KubernetesInput) {
  const secret = input.auth === "key" ? input.privateKey : input.password;
  const kubeconfig =
    input.kubeconfigSource === "path"
      ? input.kubeconfigPath
      : input.kubeconfigText;
  return Boolean(
    input.name.trim() &&
    input.host.trim() &&
    input.user.trim() &&
    secret.trim() &&
    kubeconfig.trim(),
  );
}

export function connectionChecks(input: KubernetesInput) {
  const target = `${input.user}@${input.host}:${input.port || "22"}`;
  return [
    {
      label: "SSH 접속",
      detail: `${target} (${input.auth === "key" ? "SSH 키" : "비밀번호"})`,
    },
    {
      label: "kubectl 실행",
      detail: `${input.kubeconfigSource === "path" ? input.kubeconfigPath : "붙여넣은 kubeconfig"}${input.context ? `, context ${input.context}` : ""}`,
    },
    {
      label: "클러스터 정보 읽기",
      detail: "노드/CPU/메모리/버전/StorageClass/설치된 도구",
    },
  ];
}

export const KIND_OPTIONS: {
  value: EnvKind;
  title: string;
  shell: string;
  detail: string;
  disabled?: boolean;
}[] = [
  {
    value: "kubernetes",
    title: "온프렘 Kubernetes",
    shell: "SSH + kubectl",
    detail: "k3s/kubeadm 등 직접 운영하는 클러스터",
  },
  {
    value: "aws",
    title: "AWS 계정",
    shell: "aws CLI",
    detail: "EKS/RDS/MSK 가 있는 계정",
  },
  {
    value: "gcp",
    title: "GCP 프로젝트",
    shell: "gcloud",
    detail: "준비 중이에요",
    disabled: true,
  },
];

export const ROLE_OPTIONS: { value: EnvRole; label: string }[] = [
  { value: "source", label: "출발 환경" },
  { value: "target", label: "도착 환경" },
];

export const SCOPE_OPTIONS: {
  value: AwsScope;
  label: string;
  detail: string;
}[] = [
  {
    value: "viewonly",
    label: "설정 조회만",
    detail:
      "ViewOnlyAccess + SecurityAudit. 리소스 설정과 보안 설정만 읽고 데이터 내용은 읽지 않아요.",
  },
  {
    value: "readonly",
    label: "데이터 포함 전체 읽기",
    detail: "ReadOnlyAccess. S3 객체처럼 데이터 내용까지 읽을 수 있어요.",
  },
];

export const AWS_REGIONS = [
  "ap-northeast-2",
  "ap-northeast-1",
  "us-east-1",
  "us-west-2",
  "eu-central-1",
];

export const SCRIPT_NAMES: Record<Exclude<EnvKind, "gcp">, string> = {
  kubernetes: "oneport-enroll-k8s.sh",
  aws: "oneport-enroll-aws.sh",
};

// 데모 값으로 채운 플랫폼 AWS 주체. 실제로는 서버 설정(ONEPORT_AWS_PRINCIPAL_ARN)에서 온다.
export const DEMO_AWS_PRINCIPAL =
  "arn:aws:iam::111122223333:role/oneport-platform-collector";

export const TOKEN_TTL_MINUTES = 60;

function randomText(
  bytes: number,
  alphabet = "abcdefghijklmnopqrstuvwxyz0123456789",
) {
  const values = new Uint8Array(bytes);
  crypto.getRandomValues(values);
  return Array.from(values, (v) => alphabet[v % alphabet.length]).join("");
}

export function demoTicket(now = new Date()): Ticket {
  return {
    id: `env_${randomText(12)}`,
    token: randomText(
      32,
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789",
    ),
    externalId: `oneport-${randomText(24, "0123456789abcdef")}`,
    expiresAt: new Date(now.getTime() + TOKEN_TTL_MINUTES * 60_000),
  };
}

export function scriptUrl(origin: string, kind: Exclude<EnvKind, "gcp">) {
  return `${origin}/api/v1/enrollments/scripts/${SCRIPT_NAMES[kind]}`;
}

function quote(value: string) {
  return /^[\w@%+=:,./-]+$/.test(value)
    ? value
    : `'${value.replace(/'/g, "'\\''")}'`;
}

export function commandSteps(
  kind: Exclude<EnvKind, "gcp">,
  input: KubernetesInput | AwsInput,
  ticket: Ticket,
  origin: string,
  sha256: string | null,
): CommandStep[] {
  const script = SCRIPT_NAMES[kind];
  const common = [
    `--platform ${quote(origin)}`,
    `--enrollment ${ticket.id}`,
    `--token ${ticket.token}`,
  ];
  const flags =
    kind === "kubernetes"
      ? common
      : [
          ...common,
          `--trust-principal ${DEMO_AWS_PRINCIPAL}`,
          `--external-id ${ticket.externalId}`,
          `--region ${(input as AwsInput).region}`,
          `--policy ${(input as AwsInput).scope}`,
          ...((input as AwsInput).roleName &&
          (input as AwsInput).roleName !== "oneport-readonly"
            ? [`--role-name ${quote((input as AwsInput).roleName)}`]
            : []),
          ...((input as AwsInput).boundary
            ? [`--permissions-boundary ${quote((input as AwsInput).boundary)}`]
            : []),
        ];
  return [
    {
      title: "다운로드",
      comment: "# 작업 폴더를 만들고 등록 스크립트를 받아요",
      lines: [
        "mkdir oneport-enroll && cd oneport-enroll",
        `curl -fsSLo ${script} ${scriptUrl(origin, kind)}`,
      ],
    },
    {
      title: "해시 확인",
      comment: "# 받은 스크립트가 플랫폼이 준 것과 같은지 확인해요 (선택)",
      lines: [`echo "${sha256 ?? "<계산 중>"}  ${script}" | shasum -a 256 -c`],
    },
    {
      title: "실행",
      comment:
        kind === "kubernetes"
          ? "# kubectl 이 되는 셸(클러스터 관리자 권한)에서 실행해요"
          : "# aws 명령이 되는 셸(IAM 역할을 만들 수 있는 권한)에서 실행해요",
      lines: [
        `sh ${script} \\`,
        ...flags.map((f, i) => `  ${f}${i < flags.length - 1 ? " \\" : ""}`),
      ],
    },
  ];
}

export const SCRIPT_ACTIONS: Record<Exclude<EnvKind, "gcp">, string[]> = {
  kubernetes: [
    "클러스터에 읽기 전용 계정(oneport-reader)을 만들어요. Secret 은 읽지 않아요.",
    "그 계정의 토큰으로 kubeconfig 를 만들어 플랫폼에 보내요. 지금 쓰는 관리자 kubeconfig 는 보내지 않아요.",
    "노드/버전/StorageClass/IngressClass/설치된 도구 이름을 읽어요.",
    "등록 토큰은 한 번만 쓸 수 있고 1시간 뒤 만료돼요.",
  ],
  aws: [
    "읽기 전용 IAM 역할을 만들어요. 플랫폼 주체만, 이 등록의 External ID 로만 맡을 수 있어요.",
    "액세스 키는 만들지도 보내지도 않아요. 플랫폼에는 역할 ARN 만 전달돼요.",
    "계정 번호/별칭과 리전의 EKS/RDS/VPC/MSK 개수를 읽어요.",
    "등록 토큰은 한 번만 쓸 수 있고 1시간 뒤 만료돼요.",
  ],
};

export type DemoNode = {
  name: string;
  role: "control" | "worker";
  cpu: number;
  memoryGiB: number;
  os: string;
  runtime: string;
  ip: string;
};

export type KubernetesResult = {
  kind: "kubernetes";
  context: string;
  server: string;
  version: string;
  distribution: string;
  nodes: DemoNode[];
  storageClasses: string[];
  ingressClasses: string[];
  namespaces: number;
  controllers: string[];
};

export type AwsResult = {
  kind: "aws";
  account: string;
  alias: string;
  region: string;
  roleArn: string;
  counts: { label: string; value: number }[];
};

export function demoResult(
  kind: Exclude<EnvKind, "gcp">,
  input: KubernetesInput | AwsInput,
): KubernetesResult | AwsResult {
  if (kind === "kubernetes") {
    const k8s = input as KubernetesInput;
    const server = `https://${k8s.host || "192.0.2.10"}:6443`;
    return {
      kind,
      context: k8s.context || "hackathon",
      server,
      version: "v1.35.4+k3s1",
      distribution: "k3s",
      nodes: [
        {
          name: "k3s-master",
          role: "control",
          cpu: 2,
          memoryGiB: 3.8,
          os: "Ubuntu 22.04.5 LTS",
          runtime: "containerd 2.2.3",
          ip: "192.0.2.10",
        },
        {
          name: "k3s-worker",
          role: "worker",
          cpu: 2,
          memoryGiB: 7.8,
          os: "Ubuntu 22.04.5 LTS",
          runtime: "containerd 2.2.3",
          ip: "192.0.2.11",
        },
      ],
      storageClasses: ["local-path (기본)"],
      ingressClasses: ["traefik"],
      namespaces: 10,
      controllers: [
        "Argo CD",
        "Argo Rollouts",
        "Traefik",
        "Cloudflare Tunnel",
        "Prometheus Operator",
      ],
    };
  }
  const aws = input as AwsInput;
  return {
    kind,
    account: "111122223333",
    alias: input.name || "demo-account",
    region: aws.region,
    roleArn: `arn:aws:iam::111122223333:role/${aws.roleName || "oneport-readonly"}`,
    counts: [
      { label: "EKS 클러스터", value: 1 },
      { label: "RDS 인스턴스", value: 1 },
      { label: "VPC", value: 2 },
      { label: "MSK 클러스터", value: 1 },
    ],
  };
}

export function kubernetesSummary(result: KubernetesResult) {
  const control = result.nodes.filter((n) => n.role === "control").length;
  const cpu = result.nodes.reduce((sum, n) => sum + n.cpu, 0);
  const memory = result.nodes.reduce((sum, n) => sum + n.memoryGiB, 0);
  return {
    control,
    worker: result.nodes.length - control,
    cpu,
    memory: Math.round(memory * 10) / 10,
    sentence: `노드 ${result.nodes.length}대(컨트롤 플레인 ${control}대, 워커 ${result.nodes.length - control}대)에 CPU ${cpu}코어, 메모리 ${Math.round(memory * 10) / 10}GiB예요.`,
  };
}

export async function sha256Hex(text: string) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(text),
  );
  return Array.from(new Uint8Array(digest), (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}
