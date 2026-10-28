import type { State } from './types';

export function performanceTotals(state: State): { wins: number; losses: number; winrate: number } {
  if (state.performance) {
    const wins = Math.max(0, state.performance.wins);
    const losses = Math.max(0, state.performance.losses);
    return { wins, losses, winrate: wins + losses ? wins / (wins + losses) * 100 : 0 };
  }
  const history = state.history.filter(record => record.format_id === state.config.format_id &&
    (!record.username || record.username.toLowerCase() === state.config.username.toLowerCase()));
  const wins = history.filter(record => record.result === 'win').length;
  const losses = history.filter(record => record.result === 'loss').length;
  return { wins, losses, winrate: wins + losses ? wins / (wins + losses) * 100 : 0 };
}
