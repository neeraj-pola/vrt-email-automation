import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { onAuthStateChanged, signInWithEmailAndPassword, signOut as firebaseSignOut } from "firebase/auth";
import { auth, authEnabled } from "../lib/firebase";

// The little the UI needs to know about who is signed in.
export interface Session {
  user: { email: string | null };
}

interface AuthContextValue {
  session: Session | null;
  loading: boolean;
  authEnabled: boolean;
  signIn: (email: string, password: string) => Promise<string | null>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

// Firebase's error codes, in words a staff member can act on.
function friendlyError(code: string | undefined): string {
  switch (code) {
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
    case "auth/invalid-email":
      return "Incorrect email or password.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a few minutes and try again.";
    case "auth/user-disabled":
      return "This account has been disabled.";
    case "auth/network-request-failed":
      return "Couldn't reach the sign-in service. Check your connection.";
    default:
      return "Couldn't sign in. Please try again.";
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(authEnabled);

  useEffect(() => {
    if (!auth) return;
    return onAuthStateChanged(auth, (user) => {
      setSession(user ? { user: { email: user.email } } : null);
      setLoading(false);
    });
  }, []);

  const signIn = async (email: string, password: string) => {
    if (!auth) return null;
    try {
      await signInWithEmailAndPassword(auth, email, password);
      return null;
    } catch (e) {
      return friendlyError((e as { code?: string }).code);
    }
  };

  const signOut = async () => {
    if (auth) await firebaseSignOut(auth);
  };

  return <AuthContext.Provider value={{ session, loading, authEnabled, signIn, signOut }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
