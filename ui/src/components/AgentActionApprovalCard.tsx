/**
 * Approve/Decline prompt for the assistant's agent_start / agent_stop.
 *
 * The server only acts after this click (the assistant cannot answer it itself).
 */

import { ShieldQuestion } from 'lucide-react'
import type { AgentActionApproval } from '../lib/types'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'

interface AgentActionApprovalCardProps {
  approval: AgentActionApproval
  onDecide: (id: string, approved: boolean) => void
}

function describe(approval: AgentActionApproval): string {
  if (approval.action === 'stop') {
    return 'The assistant wants to stop the coding agent. Work in progress on the current feature is interrupted.'
  }
  const agents = approval.details.max_concurrency
  const count = agents && agents > 1 ? ` with ${agents} concurrent agents` : ''
  return `The assistant wants to start the coding agent${count}, using your global settings.`
}

export function AgentActionApprovalCard({ approval, onDecide }: AgentActionApprovalCardProps) {
  return (
    <Alert>
      <ShieldQuestion />
      <AlertTitle>{approval.action === 'stop' ? 'Stop the agent?' : 'Start the agent?'}</AlertTitle>
      <AlertDescription>
        <p>{describe(approval)}</p>
        <div className="flex gap-2 mt-2">
          <Button size="sm" onClick={() => onDecide(approval.id, true)}>
            Approve
          </Button>
          <Button size="sm" variant="outline" onClick={() => onDecide(approval.id, false)}>
            Decline
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  )
}
