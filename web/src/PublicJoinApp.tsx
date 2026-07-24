import ILinkJoinPage from "@/pages/ILinkJoinPage";

export function isPublicJoinPath(pathname: string, basePath = ""): boolean {
  const normalizedBase = basePath.replace(/\/+$/, "");
  const appPath = normalizedBase && pathname.startsWith(`${normalizedBase}/`)
    ? pathname.slice(normalizedBase.length)
    : pathname;
  return appPath === "/join" || appPath === "/join/";
}

export default function PublicJoinApp() {
  return <ILinkJoinPage />;
}
