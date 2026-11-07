/**
 * Import Project Modal Component
 *
 * Multi-step wizard for importing existing projects:
 * 1. Select project folder
 * 2. Analyze and detect tech stack
 * 3. Extract features from codebase
 * 4. Review and select features to import
 * 5. Create features in database
 */

import { useState, useEffect, useRef, useCallback } from 'react'
import {
  Folder,
  Search,
  Layers,
  CheckCircle2,
  AlertCircle,
  Loader2,
  ArrowRight,
  ArrowLeft,
  Code,
  Database,
  Server,
  Layout,
  CheckSquare,
  Square,
  ChevronDown,
  ChevronRight,
} from 'lucide-react'
import { useImportProject } from '../hooks/useImportProject'
import { useCreateProject } from '../hooks/useProjects'
import { FolderBrowser } from './FolderBrowser'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

type Step = 'folder' | 'analyzing' | 'detected' | 'features' | 'register' | 'complete' | 'error'

interface ImportProjectModalProps {
  isOpen: boolean
  onClose: () => void
  onProjectImported: (projectName: string) => void
}

export function ImportProjectModal({
  isOpen,
  onClose,
  onProjectImported,
}: ImportProjectModalProps) {
  const [step, setStep] = useState<Step>('folder')
  const [projectName, setProjectName] = useState('')
  const [expandedCategories, setExpandedCategories] = useState<Set<string>>(new Set())
  const [registerError, setRegisterError] = useState<string | null>(null)
  const redirectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Track if modal is active to prevent stale state updates after close/unmount
  const isActiveRef = useRef(true)

  const {
    state,
    analyze,
    extractFeatures,
    createFeatures,
    toggleFeature,
    selectAllFeatures,
    deselectAllFeatures,
    reset,
  } = useImportProject()

  const createProject = useCreateProject()

  // Set active on mount/open, inactive on close/unmount
  useEffect(() => {
    isActiveRef.current = isOpen
    return () => {
      isActiveRef.current = false
    }
  }, [isOpen])

  // Define handleClose early with useCallback so it can be used in useEffect
  const handleClose = useCallback(() => {
    isActiveRef.current = false
    if (redirectTimeoutRef.current) {
      clearTimeout(redirectTimeoutRef.current)
      redirectTimeoutRef.current = null
    }
    setStep('folder')
    setProjectName('')
    setExpandedCategories(new Set())
    setRegisterError(null)
    reset()
    onClose()
  }, [onClose, reset])

  // Expand all categories when features are extracted
  useEffect(() => {
    if (step === 'features' && state.featuresResult) {
      setExpandedCategories(new Set(Object.keys(state.featuresResult.by_category)))
    }
  }, [step, state.featuresResult])

  // Cleanup redirect timeout on unmount
  useEffect(() => {
    return () => {
      if (redirectTimeoutRef.current) {
        clearTimeout(redirectTimeoutRef.current)
      }
    }
  }, [])

  if (!isOpen) return null

  const handleFolderSelect = async (path: string) => {
    setStep('analyzing')
    const success = await analyze(path)
    // Check if still active after async operation
    if (!isActiveRef.current) return
    if (success) {
      setStep('detected')
    } else {
      setStep('error')
    }
  }

  const handleExtractFeatures = async () => {
    const success = await extractFeatures()
    // Check if still active after async operation
    if (!isActiveRef.current) return
    if (success) {
      setStep('features')
      // Expand all categories by default - need to get fresh state via callback
      // The featuresResult will be available after the state update from extractFeatures
    } else {
      setStep('error')
    }
  }

  const handleContinueToRegister = () => {
    // Generate default project name from path
    const pathParts = state.projectPath?.split(/[/\\]/) || []
    const defaultName = pathParts[pathParts.length - 1] || 'imported-project'
    setProjectName(defaultName.replace(/[^a-zA-Z0-9_-]/g, '-'))
    setStep('register')
  }

  const handleRegisterAndCreate = async () => {
    const trimmedName = projectName.trim()
    if (!trimmedName || !state.projectPath) return

    // Validate project name format
    if (!/^[a-zA-Z0-9_-]+$/.test(trimmedName)) {
      setRegisterError('Project name can only contain letters, numbers, hyphens, and underscores')
      return
    }

    setRegisterError(null)

    try {
      // First register the project
      await createProject.mutateAsync({
        name: trimmedName,
        path: state.projectPath,
        specMethod: 'manual',
      })

      // Check if still active after async operation
      if (!isActiveRef.current) return

      // Then create features
      const success = await createFeatures(trimmedName)

      // Check if still active after async operation
      if (!isActiveRef.current) return

      if (success) {
        setStep('complete')
        redirectTimeoutRef.current = setTimeout(() => {
          if (!isActiveRef.current) return
          onProjectImported(trimmedName)
          handleClose()
        }, 1500)
      }
    } catch (err) {
      if (!isActiveRef.current) return
      setRegisterError(err instanceof Error ? err.message : 'Failed to register project')
    }
  }

  const handleBack = () => {
    if (step === 'detected' || step === 'analyzing' || step === 'error') {
      setStep('folder')
      reset()
    } else if (step === 'features') {
      setStep('detected')
    } else if (step === 'register') {
      setStep('features')
    }
  }

  const toggleCategory = (category: string) => {
    setExpandedCategories(prev => {
      const next = new Set(prev)
      if (next.has(category)) {
        next.delete(category)
      } else {
        next.add(category)
      }
      return next
    })
  }

  const getStackIcon = (category: string) => {
    switch (category.toLowerCase()) {
      case 'frontend':
        return <Layout size={16} className="text-primary" />
      case 'backend':
        return <Server size={16} className="text-[var(--color-log-success)]" />
      case 'database':
        return <Database size={16} className="text-[var(--color-log-warning)]" />
      default:
        return <Code size={16} className="text-muted-foreground" />
    }
  }

  // Shared dialog shell; Escape and outside clicks close the wizard
  const renderDialog = (className: string, content: React.ReactNode) => (
    <Dialog open={true} onOpenChange={(open) => !open && handleClose()}>
      <DialogContent className={className}>{content}</DialogContent>
    </Dialog>
  )

  // Folder selection step
  if (step === 'folder') {
    return renderDialog(
      'sm:max-w-3xl max-h-[85vh] flex flex-col p-0',
      <>
        <DialogHeader className="p-6 pb-4 border-b">
          <div className="flex items-center gap-3">
            <Folder size={24} className="text-primary" />
            <div>
              <DialogTitle>Import Existing Project</DialogTitle>
              <DialogDescription>
                Select the folder containing your existing project
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>
        <div className="flex-1 overflow-hidden">
          <FolderBrowser onSelect={handleFolderSelect} onCancel={handleClose} />
        </div>
      </>
    )
  }

  // Analyzing step
  if (step === 'analyzing' || state.step === 'analyzing') {
    return renderDialog(
      'sm:max-w-lg',
      <>
        <DialogHeader>
          <DialogTitle>Analyzing Project</DialogTitle>
        </DialogHeader>
        <div className="py-4 text-center">
          <div className="inline-flex items-center justify-center w-16 h-16 bg-primary/10 rounded-full mb-4 animate-pulse">
            <Search size={32} className="text-primary" />
          </div>
          <h3 className="font-semibold text-lg mb-2">Detecting Tech Stack</h3>
          <p className="text-muted-foreground mb-4">
            Scanning your project for frameworks, routes, and components...
          </p>
          <Loader2 size={24} className="animate-spin mx-auto text-primary" />
        </div>
      </>
    )
  }

  // Error state (check both local and hook state for consistency)
  if (step === 'error' || state.step === 'error') {
    return renderDialog(
      'sm:max-w-lg',
      <>
        <DialogHeader>
          <DialogTitle>Error</DialogTitle>
        </DialogHeader>
        <div className="py-4 text-center">
          <div className="inline-flex items-center justify-center w-16 h-16 bg-destructive/10 rounded-full mb-4">
            <AlertCircle size={32} className="text-destructive" />
          </div>
          <h3 className="font-semibold text-lg mb-2">Analysis Failed</h3>
          <p className="text-destructive mb-4">
            {state.error || 'An unexpected error occurred. Please try again.'}
          </p>
          <Button variant="outline" onClick={handleBack}>
            <ArrowLeft size={16} />
            Try Again
          </Button>
        </div>
      </>
    )
  }

  // Detection results step
  if (step === 'detected' && state.analyzeResult) {
    const result = state.analyzeResult
    return renderDialog(
      'sm:max-w-2xl max-h-[85vh] flex flex-col',
      <>
        <DialogHeader>
          <div className="flex items-center gap-3">
            <Layers size={24} className="text-primary" />
            <DialogTitle>Stack Detected</DialogTitle>
          </div>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-6">
          <Card>
            <CardContent className="p-4">
              <p className="text-sm text-muted-foreground">{result.summary}</p>
            </CardContent>
          </Card>

          <div>
            <h3 className="font-semibold mb-3">Detected Technologies</h3>
            <div className="grid grid-cols-2 gap-3">
              {result.detected_stacks.map((stack, i) => (
                <Card key={i}>
                  <CardContent className="flex items-center gap-3 p-3">
                    {getStackIcon(stack.category)}
                    <div className="flex-1 min-w-0">
                      <div className="font-semibold text-sm truncate">{stack.name}</div>
                      <div className="text-xs text-muted-foreground">{stack.category}</div>
                    </div>
                    <Badge variant="secondary" className="font-mono">
                      {Math.round(stack.confidence * 100)}%
                    </Badge>
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>

          <div>
            <h3 className="font-semibold mb-3">Codebase Analysis</h3>
            <div className="grid grid-cols-3 gap-3">
              {[
                { label: 'Routes', value: result.routes_count },
                { label: 'Endpoints', value: result.endpoints_count },
                { label: 'Components', value: result.components_count },
              ].map(({ label, value }) => (
                <Card key={label}>
                  <CardContent className="p-3 text-center">
                    <div className="text-2xl font-bold text-primary">{value}</div>
                    <div className="text-xs text-muted-foreground">{label}</div>
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        </div>

        <DialogFooter className="sm:justify-between">
          <Button variant="ghost" onClick={handleBack}>
            <ArrowLeft size={16} />
            Back
          </Button>
          <Button onClick={handleExtractFeatures} disabled={state.step === 'extracting'}>
            {state.step === 'extracting' ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                Extracting...
              </>
            ) : (
              <>
                Extract Features
                <ArrowRight size={16} />
              </>
            )}
          </Button>
        </DialogFooter>
      </>
    )
  }

  // Features review step
  if (step === 'features' && state.featuresResult) {
    const result = state.featuresResult
    const categories = Object.keys(result.by_category)

    // Group features by category
    const featuresByCategory: Record<string, typeof result.features> = {}
    result.features.forEach(f => {
      if (!featuresByCategory[f.category]) {
        featuresByCategory[f.category] = []
      }
      featuresByCategory[f.category].push(f)
    })

    return renderDialog(
      'sm:max-w-3xl max-h-[85vh] flex flex-col',
      <>
        <DialogHeader>
          <div className="flex items-center gap-3">
            <CheckSquare size={24} className="text-primary" />
            <div>
              <DialogTitle>Review Features</DialogTitle>
              <DialogDescription>
                {state.selectedFeatures.length} of {result.count} features selected
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="flex items-center gap-2 border-b pb-3">
          <Button variant="ghost" size="sm" onClick={selectAllFeatures}>
            Select All
          </Button>
          <Button variant="ghost" size="sm" onClick={deselectAllFeatures}>
            Deselect All
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {categories.map(category => (
            <div key={category} className="mb-4">
              <button
                type="button"
                onClick={() => toggleCategory(category)}
                className="flex items-center gap-2 w-full p-2 rounded-md hover:bg-muted transition-colors"
              >
                {expandedCategories.has(category) ? (
                  <ChevronDown size={16} />
                ) : (
                  <ChevronRight size={16} />
                )}
                <span className="font-semibold">{category}</span>
                <span className="text-sm text-muted-foreground">
                  ({featuresByCategory[category]?.length || 0})
                </span>
              </button>

              {expandedCategories.has(category) && (
                <div className="ml-6 mt-2 space-y-2">
                  {featuresByCategory[category]?.map((feature) => {
                    const isSelected = state.selectedFeatures.some(
                      f => f.name === feature.name && f.category === feature.category
                    )
                    return (
                      <div
                        key={`${feature.category}-${feature.name}`}
                        onClick={() => toggleFeature(feature)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            toggleFeature(feature)
                          }
                        }}
                        role="checkbox"
                        aria-checked={isSelected}
                        tabIndex={0}
                        className={cn(
                          'flex items-start gap-3 p-3 cursor-pointer rounded-md border transition-colors',
                          isSelected ? 'border-primary bg-primary/10' : 'hover:bg-muted'
                        )}
                      >
                        {isSelected ? (
                          <CheckSquare size={18} className="text-primary flex-shrink-0 mt-0.5" />
                        ) : (
                          <Square size={18} className="text-muted-foreground flex-shrink-0 mt-0.5" />
                        )}
                        <div className="flex-1 min-w-0">
                          <div className="font-medium text-sm">{feature.name}</div>
                          <div className="text-xs text-muted-foreground truncate">
                            {feature.description}
                          </div>
                          <div className="flex items-center gap-2 mt-1">
                            <Badge variant="outline" className="text-xs">
                              {feature.source_type}
                            </Badge>
                            {feature.source_file && (
                              <span className="text-xs text-muted-foreground font-mono truncate">
                                {feature.source_file}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          ))}
        </div>

        <DialogFooter className="sm:justify-between">
          <Button variant="ghost" onClick={handleBack}>
            <ArrowLeft size={16} />
            Back
          </Button>
          <Button onClick={handleContinueToRegister} disabled={state.selectedFeatures.length === 0}>
            Continue
            <ArrowRight size={16} />
          </Button>
        </DialogFooter>
      </>
    )
  }

  // Register project step
  if (step === 'register') {
    return renderDialog(
      'sm:max-w-lg',
      <>
        <DialogHeader>
          <DialogTitle>Register Project</DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="import-project-name">Project Name</Label>
            <Input
              id="import-project-name"
              type="text"
              value={projectName}
              onChange={(e) => setProjectName(e.target.value)}
              placeholder="my-project"
              pattern="^[a-zA-Z0-9_\-]+$"
              autoFocus
            />
            <p className="text-sm text-muted-foreground">
              Use letters, numbers, hyphens, and underscores only.
            </p>
          </div>

          <Card>
            <CardContent className="p-4 text-sm space-y-1">
              <div className="flex justify-between">
                <span>Features to create:</span>
                <span className="font-semibold">{state.selectedFeatures.length}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span>Project path:</span>
                <span className="font-mono text-xs truncate max-w-[200px]">{state.projectPath}</span>
              </div>
            </CardContent>
          </Card>

          {(registerError || state.error) && (
            <Alert variant="destructive">
              <AlertDescription>{registerError || state.error}</AlertDescription>
            </Alert>
          )}
        </div>

        <DialogFooter className="sm:justify-between">
          <Button variant="ghost" onClick={handleBack}>
            <ArrowLeft size={16} />
            Back
          </Button>
          <Button
            onClick={handleRegisterAndCreate}
            disabled={!projectName.trim() || createProject.isPending || state.step === 'creating'}
          >
            {createProject.isPending || state.step === 'creating' ? (
              <>
                <Loader2 size={16} className="animate-spin" />
                Creating...
              </>
            ) : (
              <>
                Import Project
                <CheckCircle2 size={16} />
              </>
            )}
          </Button>
        </DialogFooter>
      </>
    )
  }

  // Complete step
  if (step === 'complete') {
    return renderDialog(
      'sm:max-w-lg',
      <>
        <DialogHeader>
          <DialogTitle>Import Complete</DialogTitle>
        </DialogHeader>
        <div className="py-4 text-center">
          <div className="inline-flex items-center justify-center w-16 h-16 bg-primary/10 rounded-full mb-4">
            <CheckCircle2 size={32} className="text-primary" />
          </div>
          <h3 className="font-semibold text-xl mb-2">{projectName}</h3>
          <p className="text-muted-foreground mb-2">Project imported successfully!</p>
          <p className="text-sm text-muted-foreground">
            {state.createResult?.created} features created
          </p>
          <div className="mt-4 flex items-center justify-center gap-2">
            <Loader2 size={16} className="animate-spin" />
            <span className="text-sm">Redirecting...</span>
          </div>
        </div>
      </>
    )
  }

  return null
}
