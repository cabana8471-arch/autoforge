import { useState } from 'react'
import { ChevronDown, Plus, FolderOpen, Loader2, Trash2, Unlink, Link2 } from 'lucide-react'
import type { ProjectSummary } from '../lib/types'
import { NewProjectModal } from './NewProjectModal'
import { ConfirmDialog } from './ConfirmDialog'
import { useDeleteProject, useDetachProject, useReattachProject } from '../hooks/useProjects'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

interface ProjectSelectorProps {
  projects: ProjectSummary[]
  selectedProject: string | null
  onSelectProject: (name: string | null) => void
  isLoading: boolean
  onSpecCreatingChange?: (isCreating: boolean) => void
}

export function ProjectSelector({
  projects,
  selectedProject,
  onSelectProject,
  isLoading,
  onSpecCreatingChange,
}: ProjectSelectorProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [showNewProjectModal, setShowNewProjectModal] = useState(false)
  const [projectToDelete, setProjectToDelete] = useState<string | null>(null)
  const [projectToDetach, setProjectToDetach] = useState<string | null>(null)
  const [projectToReattach, setProjectToReattach] = useState<string | null>(null)

  const deleteProject = useDeleteProject()
  const detachProject = useDetachProject()
  const reattachProject = useReattachProject()

  const handleProjectCreated = (projectName: string) => {
    onSelectProject(projectName)
    setIsOpen(false)
  }

  const handleDeleteClick = (e: React.MouseEvent, projectName: string) => {
    e.stopPropagation()
    e.preventDefault()
    setProjectToDelete(projectName)
  }

  const handleConfirmDelete = async () => {
    if (!projectToDelete) return

    try {
      await deleteProject.mutateAsync(projectToDelete)
      if (selectedProject === projectToDelete) {
        onSelectProject(null)
      }
      setProjectToDelete(null)
    } catch (error) {
      console.error('Failed to delete project:', error)
      setProjectToDelete(null)
    }
  }

  const handleCancelDelete = () => {
    setProjectToDelete(null)
  }

  const handleDetachClick = (e: React.MouseEvent, projectName: string) => {
    e.stopPropagation()
    e.preventDefault()
    setProjectToDetach(projectName)
  }

  const handleReattachClick = (e: React.MouseEvent, projectName: string) => {
    e.stopPropagation()
    e.preventDefault()
    setProjectToReattach(projectName)
  }

  const handleConfirmDetach = async () => {
    if (!projectToDetach) return
    try {
      await detachProject.mutateAsync(projectToDetach)
      setProjectToDetach(null)
    } catch (error) {
      console.error('Failed to detach project:', error)
      setProjectToDetach(null)
    }
  }

  const handleConfirmReattach = async () => {
    if (!projectToReattach) return
    try {
      await reattachProject.mutateAsync(projectToReattach)
      setProjectToReattach(null)
    } catch (error) {
      console.error('Failed to reattach project:', error)
      setProjectToReattach(null)
    }
  }

  const selectedProjectData = projects.find(p => p.name === selectedProject)

  return (
    <div className="relative">
      <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            variant="outline"
            className="min-w-[140px] sm:min-w-[200px] justify-between"
            disabled={isLoading}
            title={selectedProjectData?.path}
          >
            {isLoading ? (
              <Loader2 size={18} className="animate-spin" />
            ) : selectedProject ? (
              <>
                <span className="flex items-center gap-2 truncate">
                  <FolderOpen size={18} className="shrink-0" />
                  <span className="truncate">{selectedProject}</span>
                </span>
                {selectedProjectData && selectedProjectData.stats.total > 0 && (
                  <Badge className="ml-2">{selectedProjectData.stats.percentage}%</Badge>
                )}
              </>
            ) : (
              <span className="text-muted-foreground">Select Project</span>
            )}
            <ChevronDown size={18} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
          </Button>
        </DropdownMenuTrigger>

        <DropdownMenuContent align="start" className="w-[280px] p-0 flex flex-col">
          {projects.length > 0 ? (
            <div className="max-h-[300px] overflow-y-auto p-1">
              {projects.map(project => (
                <DropdownMenuItem
                  key={project.name}
                  title={project.path}
                  className={`flex items-center justify-between cursor-pointer ${
                    project.name === selectedProject ? 'bg-primary/10' : ''
                  }`}
                  onSelect={() => {
                    onSelectProject(project.name)
                  }}
                >
                  <span className="flex items-center gap-2 flex-1 min-w-0">
                    <FolderOpen size={16} className="shrink-0" />
                    <span className="truncate">{project.name}</span>
                    {project.is_detached && (
                      <Badge variant="secondary" className="text-[10px] px-1 py-0 shrink-0">DETACHED</Badge>
                    )}
                    {!project.is_detached && project.stats.total > 0 && (
                      <span className="text-sm font-mono text-muted-foreground ml-auto shrink-0">
                        {project.stats.passing}/{project.stats.total}
                      </span>
                    )}
                  </span>
                  <div className="flex items-center gap-1 shrink-0 ml-2">
                    {project.is_detached ? (
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        onClick={(e: React.MouseEvent) => handleReattachClick(e, project.name)}
                        className="text-muted-foreground hover:text-primary"
                        title="Reattach project"
                      >
                        <Link2 size={14} />
                      </Button>
                    ) : (
                      <Button
                        variant="ghost"
                        size="icon-xs"
                        onClick={(e: React.MouseEvent) => handleDetachClick(e, project.name)}
                        className="text-muted-foreground hover:text-orange-500"
                        title="Detach project"
                      >
                        <Unlink size={14} />
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon-xs"
                      onClick={(e: React.MouseEvent) => handleDeleteClick(e, project.name)}
                      className="text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 size={14} />
                    </Button>
                  </div>
                </DropdownMenuItem>
              ))}
            </div>
          ) : (
            <div className="p-4 text-center text-muted-foreground">
              No projects yet
            </div>
          )}

          <DropdownMenuSeparator className="my-0" />

          <div className="p-1">
            <DropdownMenuItem
              onSelect={() => {
                setShowNewProjectModal(true)
              }}
              className="cursor-pointer font-semibold"
            >
              <Plus size={16} />
              New Project
            </DropdownMenuItem>
          </div>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* New Project Modal */}
      <NewProjectModal
        isOpen={showNewProjectModal}
        onClose={() => setShowNewProjectModal(false)}
        onProjectCreated={handleProjectCreated}
        onStepChange={(step) => onSpecCreatingChange?.(step === 'chat')}
      />

      {/* Delete Confirmation Dialog */}
      <ConfirmDialog
        isOpen={projectToDelete !== null}
        title="Delete Project"
        message={`Are you sure you want to remove "${projectToDelete}" from the registry? This will unregister the project but preserve its files on disk.`}
        confirmLabel="Delete"
        cancelLabel="Cancel"
        variant="danger"
        isLoading={deleteProject.isPending}
        onConfirm={handleConfirmDelete}
        onCancel={handleCancelDelete}
      />

      {/* Detach Confirmation Dialog */}
      <ConfirmDialog
        isOpen={projectToDetach !== null}
        title="Detach Project"
        message={`Detach "${projectToDetach}"? This will move all AutoForge files (features database, prompts, settings) to a backup directory, leaving only your application code. You can reattach later to restore everything.`}
        confirmLabel="Detach"
        cancelLabel="Cancel"
        variant="danger"
        isLoading={detachProject.isPending}
        onConfirm={handleConfirmDetach}
        onCancel={() => setProjectToDetach(null)}
      />

      {/* Reattach Confirmation Dialog */}
      <ConfirmDialog
        isOpen={projectToReattach !== null}
        title="Reattach Project"
        message={`Reattach "${projectToReattach}"? This will restore all AutoForge files (features database, prompts, settings) from the backup directory.`}
        confirmLabel="Reattach"
        cancelLabel="Cancel"
        variant="warning"
        isLoading={reattachProject.isPending}
        onConfirm={handleConfirmReattach}
        onCancel={() => setProjectToReattach(null)}
      />

    </div>
  )
}
