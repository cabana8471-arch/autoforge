/**
 * Allowed Commands Card
 *
 * Edits the selected project's allowlist (.autoforge/allowed_commands.yaml).
 * The agent cannot change this file; the server checks the text with the
 * security hook's rules before saving it.
 */

import { useState } from 'react'
import { Loader2 } from 'lucide-react'
import { useAllowedCommands, useUpdateAllowedCommands } from '../hooks/useAllowedCommands'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Textarea } from '@/components/ui/textarea'

const EXAMPLE = `version: 1
commands:
  - name: swift
    description: Swift compiler
  - name: ./scripts/build.sh
    description: Project build script`

interface AllowedCommandsCardProps {
  projectName: string | null
}

export function AllowedCommandsCard({ projectName }: AllowedCommandsCardProps) {
  const { data: file, isLoading, isError, error } = useAllowedCommands(projectName)
  const update = useUpdateAllowedCommands(projectName ?? '')
  // null = no local edits; the text shown is the saved file
  const [draft, setDraft] = useState<string | null>(null)
  const value = draft ?? file?.content ?? ''
  const dirty = draft !== null && draft !== (file?.content ?? '')

  const save = () => {
    update.mutate(value, { onSuccess: () => setDraft(null) })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Allowed Commands</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {!projectName ? (
          <p className="text-sm text-muted-foreground">Select a project to edit its command allowlist.</p>
        ) : isLoading ? (
          <Loader2 size={18} className="animate-spin text-muted-foreground" />
        ) : isError ? (
          <Alert variant="destructive">
            <AlertDescription>
              <p>Could not read the allowlist: {error instanceof Error ? error.message : 'unknown error'}</p>
            </AlertDescription>
          </Alert>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Extra bash commands the agent may run in {projectName}, on top of the global allowlist. Blocklisted
              commands (sudo, dd, shutdown, ...) cannot be added. Saved to <code>.autoforge/allowed_commands.yaml</code>;
              the agent's next command uses it.
            </p>
            {file && file.path !== '.autoforge/allowed_commands.yaml' && (
              <p className="text-sm text-muted-foreground">
                Showing the legacy file <code>{file.path}</code>; saving writes the new location.
              </p>
            )}
            <Textarea
              aria-label="allowed_commands.yaml"
              value={value}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={EXAMPLE}
              spellCheck={false}
              className="font-mono text-sm min-h-[180px]"
            />
            {update.isError && (
              <Alert variant="destructive">
                <AlertDescription>
                  <p>{update.error instanceof Error ? update.error.message : 'Failed to save the allowlist.'}</p>
                </AlertDescription>
              </Alert>
            )}
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={save} disabled={!dirty || update.isPending}>
                {update.isPending && <Loader2 size={14} className="animate-spin" />}
                Save allowlist
              </Button>
              {dirty && (
                <Button size="sm" variant="outline" onClick={() => setDraft(null)} disabled={update.isPending}>
                  Discard changes
                </Button>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
