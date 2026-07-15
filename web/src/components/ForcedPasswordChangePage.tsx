import { useState, type FormEvent } from "react";
import { KeyRound, LogOut } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api, HERMES_BASE_PATH } from "@/lib/api";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";

/** Blocks dashboard access until a temporary local-account password is replaced. */
export function ForcedPasswordChangePage() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const clearPasswords = () => {
    setCurrentPassword("");
    setNewPassword("");
    setConfirmation("");
  };


  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (newPassword !== confirmation) {
      setError("New passwords do not match.");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await api.changePassword(currentPassword, newPassword);
      clearPasswords();
      window.location.assign(`${HERMES_BASE_PATH}/login`);
    } catch (reason) {
      clearPasswords();
      setError(reason instanceof Error ? reason.message : "Could not change password.");
    } finally {
      setSubmitting(false);
    }
  };

  const logout = () => {
    dashboardAuthTransition.reset();
    void api.logout();
  };

  return (
    <main className="flex min-h-dvh items-center justify-center bg-background-base px-4 text-text-primary">
      <Card className="w-full max-w-md">
        <CardContent className="flex flex-col gap-6 py-7">
          <div className="flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-warning/10 text-warning">
              <KeyRound className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-semibold">Change your password</h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Your account has a temporary password. Choose a new password before continuing.
              </p>
            </div>
          </div>

          <form className="grid gap-4" onSubmit={(event) => void submit(event)}>
            <div className="grid gap-2">
              <Label htmlFor="forced-current-password">Current password</Label>
              <Input
                autoComplete="current-password"
                id="forced-current-password"
                required
                type="password"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="forced-new-password">New password</Label>
              <Input
                autoComplete="new-password"
                id="forced-new-password"
                required
                type="password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="forced-confirm-password">Confirm new password</Label>
              <Input
                autoComplete="new-password"
                id="forced-confirm-password"
                required
                type="password"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </div>
            {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
            <Button className="uppercase" disabled={submitting} type="submit" prefix={submitting ? <Spinner /> : undefined}>
              {submitting ? "Saving" : "Change password"}
            </Button>
          </form>

          <Button ghost size="sm" onClick={logout} prefix={<LogOut className="h-3.5 w-3.5" />}>
            Log out
          </Button>
        </CardContent>
      </Card>
    </main>
  );
}
