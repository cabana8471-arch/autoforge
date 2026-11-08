import { useEffect, useMemo, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { AlertCircle, AlertTriangle, CheckCircle2, ExternalLink, Github, Loader2, Search } from 'lucide-react'
import { getGitHubRepo, importGitHubIssues, listGitHubIssues } from '../lib/api'
import type { GitHubIssue } from '../lib/types'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Checkbox } from '@/components/ui/checkbox'
import { Alert, AlertDescription } from '@/components/ui/alert'

interface GitHubImportModalProps {
  projectName: string
  onClose: () => void
}

type IssueState = 'open' | 'closed' | 'all'

/**
 * Import issues from a GitHub repository as pending features.
 *
 * The repository is detected from the project's git remote when possible.
 * Each selected issue becomes one feature (task-list items become its steps).
 */
export function GitHubImportModal({ projectName, onClose }: GitHubImportModalProps) {
  const queryClient = useQueryClient()
  const [repo, setRepo] = useState('')
  const [issueState, setIssueState] = useState<IssueState>('open')
  const [loadedRepo, setLoadedRepo] = useState<string | null>(null)
  const [issues, setIssues] = useState<GitHubIssue[]>([])
  const [authenticated, setAuthenticated] = useState<boolean | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  // Issue whose external author the user is being asked to confirm
  const [confirming, setConfirming] = useState<number | null>(null)
  const [filter, setFilter] = useState('')
  // Off by default: anyone who can comment on an issue could steer the agent
  const [includeComments, setIncludeComments] = useState(false)
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<string | null>(null)

  const loadIssues = async (repoName: string, state: IssueState = issueState) => {
    if (!repoName.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await listGitHubIssues(projectName, repoName.trim(), state)
      setLoadedRepo(data.repo)
      setRepo(data.repo)
      setIssues(data.issues)
      setAuthenticated(data.authenticated)
      setSelected(new Set())
      setConfirming(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load issues')
      setIssues([])
      setLoadedRepo(null)
    } finally {
      setLoading(false)
    }
  }

  // Detect the repository from the project's git remote on open
  useEffect(() => {
    let cancelled = false
    getGitHubRepo(projectName)
      .then((info) => {
        if (cancelled) return
        setAuthenticated(info.authenticated)
        if (info.repo) {
          setRepo(info.repo)
          loadIssues(info.repo)
        }
      })
      .catch(() => { /* user can still type the repo */ })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectName])

  const visibleIssues = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return issues
    return issues.filter(i =>
      i.title.toLowerCase().includes(q) ||
      String(i.number) === q.replace('#', '') ||
      i.labels.some(l => l.toLowerCase().includes(q))
    )
  }, [issues, filter])

  // "Select all" only takes issues by people with write access; the others need a confirmation each
  const selectable = visibleIssues.filter(i => i.imported_feature_id === null && i.author_trusted)
  const allSelected = selectable.length > 0 && selectable.every(i => selected.has(i.number))

  const toggle = (issue: GitHubIssue) => {
    const num = issue.number
    if (!issue.author_trusted && !selected.has(num)) {
      setConfirming(num)
      return
    }
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(num)) next.delete(num)
      else next.add(num)
      return next
    })
  }

  const toggleAll = () => {
    setSelected(prev => {
      const next = new Set(prev)
      if (allSelected) selectable.forEach(i => next.delete(i.number))
      else selectable.slice(0, 50).forEach(i => next.add(i.number))
      return next
    })
  }

  const handleImport = async () => {
    if (!loadedRepo || selected.size === 0) return
    setImporting(true)
    setError(null)
    setResult(null)
    try {
      const numbers = [...selected].slice(0, 50)
      // Untrusted issues only enter the selection through the confirmation prompt
      const external = issues.filter(i => !i.author_trusted && numbers.includes(i.number)).map(i => i.number)
      const res = await importGitHubIssues(projectName, loadedRepo, numbers, includeComments, external)
      queryClient.invalidateQueries({ queryKey: ['features', projectName] })
      const imported = new Map(res.features.map(f => [Number(f.name.match(/^#(\d+)/)?.[1]), f.id]))
      setIssues(prev => prev.map(i => imported.has(i.number) ? { ...i, imported_feature_id: imported.get(i.number)! } : i))
      setSelected(new Set())
      setResult(
        `Imported ${res.created} issue${res.created === 1 ? '' : 's'} as pending features` +
        (res.skipped.length ? ` (${res.skipped.length} already imported)` : '') + '.'
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to import issues')
    } finally {
      setImporting(false)
    }
  }

  return (
    <Dialog open={true} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Github size={20} />
            Import GitHub Issues
          </DialogTitle>
          <DialogDescription>
            Turn issues from your repository into pending features for the coding agent.
          </DialogDescription>
        </DialogHeader>

        <Alert>
          <AlertTriangle className="h-4 w-4" />
          <AlertDescription>
            The imported issue text becomes instructions for the coding agent, which can run commands in
            this project. Import only issues you have read and trust; issue comments can be written by
            anyone, so they are left out unless you include them below.
          </AlertDescription>
        </Alert>

        <form
          className="flex flex-col sm:flex-row gap-2 sm:items-end"
          onSubmit={(e) => { e.preventDefault(); loadIssues(repo) }}
        >
          <div className="flex-1 space-y-2">
            <Label htmlFor="github-repo">Repository</Label>
            <Input
              id="github-repo"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="owner/name"
              autoComplete="off"
            />
          </div>
          <select
            aria-label="Issue state"
            className="h-9 rounded-md border bg-background px-3 text-sm"
            value={issueState}
            onChange={(e) => {
              const s = e.target.value as IssueState
              setIssueState(s)
              if (loadedRepo) loadIssues(loadedRepo, s)
            }}
          >
            <option value="open">Open</option>
            <option value="closed">Closed</option>
            <option value="all">All</option>
          </select>
          <Button type="submit" disabled={loading || !repo.trim()}>
            {loading ? <Loader2 size={16} className="animate-spin" /> : <Search size={16} />}
            Load issues
          </Button>
        </form>

        {authenticated === false && (
          <p className="text-xs text-muted-foreground">
            Not authenticated with GitHub: only public repositories work, with a low rate limit.
            Set <code>GITHUB_TOKEN</code> in your environment or run <code>gh auth login</code>.
          </p>
        )}

        {error && (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {result && (
          <Alert>
            <CheckCircle2 className="h-4 w-4" />
            <AlertDescription>{result}</AlertDescription>
          </Alert>
        )}

        {loadedRepo && (
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter by title, #number or label"
                className="flex-1"
                aria-label="Filter issues"
              />
              <Button type="button" variant="outline" size="sm" onClick={toggleAll} disabled={selectable.length === 0}>
                {allSelected ? 'Clear' : 'Select all'}
              </Button>
            </div>

            <div className="max-h-[40vh] overflow-y-auto border rounded-md divide-y">
              {visibleIssues.length === 0 ? (
                <div className="p-6 text-center text-muted-foreground text-sm">
                  {loading ? 'Loading…' : 'No issues found'}
                </div>
              ) : (
                visibleIssues.map(issue => {
                  const imported = issue.imported_feature_id !== null
                  const checkboxId = `gh-issue-${issue.number}`
                  return (
                    <div key={issue.number} className={`flex items-start gap-3 p-3 ${imported ? 'opacity-60' : ''}`}>
                      <Checkbox
                        id={checkboxId}
                        checked={selected.has(issue.number)}
                        disabled={imported}
                        onCheckedChange={() => toggle(issue)}
                        className="mt-1"
                      />
                      <div className="flex-1 min-w-0">
                        <label htmlFor={checkboxId} className="font-medium cursor-pointer break-words">
                          <span className="text-muted-foreground font-mono mr-1">#{issue.number}</span>
                          {issue.title}
                        </label>
                        <div className="flex flex-wrap items-center gap-1 mt-1">
                          {issue.state === 'closed' && <Badge variant="outline">closed</Badge>}
                          {imported && <Badge variant="secondary">already imported</Badge>}
                          {!issue.author_trusted && (
                            <Badge variant="destructive" title={`author_association: ${issue.author_association}`}>
                              external author{issue.author ? `: @${issue.author}` : ''}
                            </Badge>
                          )}
                          {issue.labels.map(l => <Badge key={l} variant="outline">{l}</Badge>)}
                        </div>
                        {confirming === issue.number && (
                          <div className="mt-2 rounded-md border border-destructive/50 p-2 text-sm space-y-2">
                            <p>
                              {issue.author ? `@${issue.author}` : 'The author'} has no write access to this
                              repository. Anyone can open an issue, and its text becomes instructions for the
                              coding agent. Select it only if you have read it and trust it.
                            </p>
                            <div className="flex gap-2">
                              <Button
                                type="button"
                                size="sm"
                                variant="destructive"
                                onClick={() => {
                                  setSelected(prev => new Set(prev).add(issue.number))
                                  setConfirming(null)
                                }}
                              >
                                Select anyway
                              </Button>
                              <Button type="button" size="sm" variant="outline" onClick={() => setConfirming(null)}>
                                Cancel
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>
                      <a
                        href={issue.url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-muted-foreground hover:text-foreground"
                        title="Open on GitHub"
                      >
                        <ExternalLink size={16} />
                      </a>
                    </div>
                  )
                })
              )}
            </div>

            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={includeComments}
                onCheckedChange={(v) => setIncludeComments(v === true)}
              />
              Include issue comments in the feature description (text from anyone who commented)
            </label>
          </div>
        )}

        <DialogFooter className="pt-4 border-t">
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
          <Button type="button" onClick={handleImport} disabled={!loadedRepo || selected.size === 0 || importing}>
            {importing && <Loader2 size={16} className="animate-spin" />}
            Import {selected.size > 0 ? selected.size : ''} as features
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
