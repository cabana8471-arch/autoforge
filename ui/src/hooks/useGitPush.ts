/**
 * React Query hooks for per-project git auto-push
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import * as api from '../lib/api'

export function useGitPush(projectName: string | null) {
  return useQuery({
    queryKey: ['git-push', projectName],
    queryFn: () => api.getGitPush(projectName!),
    enabled: !!projectName,
  })
}

export function useUpdateGitPush(projectName: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (enabled: boolean) => api.updateGitPush(projectName, enabled),
    onSuccess: (data) => {
      queryClient.setQueryData(['git-push', projectName], data)
    },
  })
}
