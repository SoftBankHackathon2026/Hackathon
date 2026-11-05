import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import type { Access } from "../api/types";
import { LoginPage } from "../features/auth/LoginPage";
import { useSession } from "../features/auth/useSession";
import { createQueryClient } from "./queryClient";
import { Workspace } from "./Workspace";

export function App() {
  const [client] = useState(createQueryClient);
  return (
    <QueryClientProvider client={client}>
      <SessionGate />
    </QueryClientProvider>
  );
}

function SessionGate() {
  const session = useSession();
  if (!session.access)
    return (
      <div className="login">
        <p className={session.connecting ? "banner" : "empty"}>
          {session.connecting
            ? "플랫폼 서버에 연결하는 중이에요. 잠시 후 자동으로 다시 시도해요."
            : "불러오는 중이에요…"}
        </p>
      </div>
    );
  const { access } = session;
  if (access.auth_mode === "login" && !access.user)
    return <LoginPage onSignedIn={session.signedIn} />;
  return (
    <SignedInWorkspace
      key={access.user?.email ?? "shared"}
      access={access}
      onSignOut={session.signOut}
    />
  );
}

function SignedInWorkspace({
  access,
  onSignOut,
}: {
  access: Access;
  onSignOut: () => void;
}) {
  const [client] = useState(createQueryClient);
  return (
    <QueryClientProvider client={client}>
      <Workspace access={access} onSignOut={onSignOut} />
    </QueryClientProvider>
  );
}
