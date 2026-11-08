/**
 * React Query hooks for the project allowlist (.autoforge/allowed_commands.yaml)
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as api from '../lib/api'

export function useAllowedCommands(projectName: string | null) {
  return useQuery({
    queryKey: ['allowed-commands', projectName],
    queryFn: () => api.getAllowedCommands(projectName!),
    enabled: !!projectName,
  })
}

export function useUpdateAllowedCommands(projectName: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (content: string) => api.updateAllowedCommands(projectName, content),
    onSuccess: (data) => {
      queryClient.setQueryData(['allowed-commands', projectName], data)
    },
  })
}
