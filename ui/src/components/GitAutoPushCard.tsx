/**
 * Git Auto-Push Card
 *
 * Per-project opt-in for pushing the agent's commits after each successful
 * coding session. Turning it on trusts the remote URL shown here; pushes only
 * go to that URL and stop if the remote changes or the repo's git config could
 * redirect them.
 */

import { AlertTriangle, Loader2 } from 'lucide-react'
import { useGitPush, useUpdateGitPush } from '../hooks/useGitPush'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

interface GitAutoPushCardProps {
  projectName: string | null
}

export function GitAutoPushCard({ projectName }: GitAutoPushCardProps) {
  const { data: status, isLoading } = useGitPush(projectName)
  const update = useUpdateGitPush(projectName ?? '')
  const changed = status?.enabled && status.url !== status.current_url

  return (
    <Card>
      <CardHeader>
        <CardTitle>Version Control</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!projectName ? (
          <p className="text-sm text-muted-foreground">Select a project to set up auto-push for it.</p>
        ) : (
          <>
            <div className="flex items-center justify-between gap-4">
              <div className="space-y-0.5">
                <Label htmlFor="git-auto-push" className="font-medium">
                  Auto-push {projectName} to its git remote
                </Label>
                <p className="text-sm text-muted-foreground">
                  After each successful coding session, push the current branch to the URL below. Never
                  force-pushes; credentials must already work (e.g. <code>gh auth setup-git</code> or an SSH key).
                </p>
              </div>
              {isLoading || update.isPending ? (
                <Loader2 size={18} className="animate-spin text-muted-foreground" />
              ) : (
                <Switch
                  id="git-auto-push"
                  checked={status?.enabled ?? false}
                  onCheckedChange={(checked) => update.mutate(checked)}
                />
              )}
            </div>

            {status && (
              <p className="text-sm">
                <span className="text-muted-foreground">
                  {status.enabled ? 'Pushes go to' : `Remote "${status.remote ?? 'origin'}" points to`}:
                </span>{' '}
                <code className="break-all">{(status.enabled ? status.url : status.current_url) ?? 'no such remote'}</code>
              </p>
            )}

            {changed && (
              <Alert variant="destructive">
                <AlertTriangle className="h-4 w-4" />
                <AlertDescription>
                  The remote now points to <code className="break-all">{status?.current_url ?? '(none)'}</code>, so
                  pushes are refused. If you trust the new URL, turn auto-push off and on again.
                </AlertDescription>
              </Alert>
            )}

            {status && status.unsafe_config.length > 0 && (
              <Alert variant="destructive">
                <AlertTriangle className="h-4 w-4" />
                <AlertDescription>
                  The project&apos;s .git/config sets {status.unsafe_config.join(', ')}, which can redirect a push
                  or run a program, so auto-push is refused. Remove them or move them to your global git config.
                </AlertDescription>
              </Alert>
            )}

            {update.error && (
              <Alert variant="destructive">
                <AlertDescription>{update.error.message}</AlertDescription>
              </Alert>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
