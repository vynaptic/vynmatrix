"use client";

import { useSyncExternalStore } from "react";
import { LoginModal } from "./login-modal";
import { isAuthenticated, subscribeAuthentication } from "@/lib/api-client";

const serverAuthentication = () => false;

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const authenticated = useSyncExternalStore(
    subscribeAuthentication, isAuthenticated, serverAuthentication,
  );

  if (!authenticated) {
    return <LoginModal />;
  }

  return <>{children}</>;
}
