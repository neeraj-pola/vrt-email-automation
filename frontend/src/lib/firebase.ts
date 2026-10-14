// Staff login (CLAUDE.md §9): Firebase Auth backs employee accounts so
// multiple staff can use the dashboard in parallel, each with their own
// credentials. The browser only uses Firebase for sign-in; all data goes
// through the backend API, which verifies the ID token this produces.
import { initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

const config = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY as string | undefined,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN as string | undefined,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID as string | undefined,
  appId: import.meta.env.VITE_FIREBASE_APP_ID as string | undefined,
};

// Not configured (local/mock mode): the app runs without a login screen.
export const authEnabled = Boolean(config.apiKey && config.authDomain && config.projectId && config.appId);

export const auth = authEnabled ? getAuth(initializeApp(config)) : null;

// A fresh ID token (Firebase refreshes it itself when it is close to expiring).
export async function getIdToken(): Promise<string | null> {
  return auth?.currentUser ? auth.currentUser.getIdToken() : null;
}
