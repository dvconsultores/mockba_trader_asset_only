"""
Signal History Analyzer - Query and analyze stored trading signals
Run periodically to evaluate strategy performance and filter effectiveness

Usage:
    python signal_analyzer.py [--days 7] [--pattern 2-candle] [--approved-only]
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
import sqlite3

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db.db_ops import get_db_connection
from logs.log_config import apolo_trader_logger as logger


class SignalAnalyzer:
    """Analyze signal history for strategy optimization."""
    
    def __init__(self, days_back: int = 7):
        """Initialize analyzer with lookback period."""
        self.days_back = days_back
        self.utc_now = datetime.now(timezone.utc)
        self.user_now = self.utc_now - timedelta(hours=4)
        self.cutoff_date = (self.user_now - timedelta(days=days_back)).isoformat()
        
    def get_signals(self, approved_only: bool = False, limit: int = 1000):
        """Fetch signals from database."""
        with get_db_connection() as conn:
            cur = conn.cursor()
            query = "SELECT * FROM signal_history WHERE timestamp > ? "
            params = [self.cutoff_date]
            
            if approved_only:
                query += "AND approved = 1 "
            
            query += "ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            cur.execute(query, params)
            rows = cur.fetchall()
            
            signals = []
            for row in rows:
                sig = dict(row)
                # Parse JSON fields
                sig['rejection_reasons'] = json.loads(sig['rejection_reasons'] or '[]')
                sig['manipulation_warnings'] = json.loads(sig['manipulation_warnings'] or '[]')
                signals.append(sig)
            
            return signals
    
    def analyze_approval_rate(self):
        """Analyze signal approval rate."""
        signals = self.get_signals(limit=10000)
        
        if not signals:
            return {"error": "No signals found"}
        
        total = len(signals)
        approved = sum(1 for s in signals if s['approved'])
        rejected = total - approved
        
        return {
            'total_signals': total,
            'approved_count': approved,
            'rejected_count': rejected,
            'approval_rate': f"{(approved/total)*100:.1f}%",
            'time_period_days': self.days_back,
            'cutoff_date': self.cutoff_date,
        }
    
    def analyze_rejection_reasons(self):
        """Analyze why signals were rejected."""
        signals = self.get_signals(approved_only=False, limit=10000)
        rejected = [s for s in signals if not s['approved']]
        
        if not rejected:
            return {"message": "No rejected signals found"}
        
        reason_counts = Counter()
        for sig in rejected:
            reasons = sig['rejection_reasons']
            for reason in reasons:
                # Normalize reason strings
                if isinstance(reason, str):
                    reason_counts[reason] += 1
        
        return {
            'total_rejected': len(rejected),
            'rejection_reasons': dict(reason_counts.most_common(10)),
        }
    
    def analyze_by_pattern(self):
        """Analysis signals by detected pattern type."""
        signals = self.get_signals(limit=10000)
        
        pattern_stats = defaultdict(lambda: {'approved': 0, 'total': 0})
        
        for sig in signals:
            pattern = sig['pattern_type'] or 'None'
            pattern_stats[pattern]['total'] += 1
            if sig['approved']:
                pattern_stats[pattern]['approved'] += 1
        
        result = {}
        for pattern, stats in sorted(pattern_stats.items(), key=lambda x: x[1]['total'], reverse=True):
            approval_pct = (stats['approved'] / stats['total'] * 100) if stats['total'] > 0 else 0
            result[pattern] = {
                'total': stats['total'],
                'approved': stats['approved'],
                'approval_rate': f"{approval_pct:.1f}%"
            }
        
        return result
    
    def analyze_by_regime(self):
        """Analysis signals by market regime."""
        signals = self.get_signals(limit=10000)
        
        regime_stats = defaultdict(lambda: {'approved': 0, 'total': 0, 'avg_obi': []})
        
        for sig in signals:
            regime = sig['regime'] or 'Unknown'
            regime_stats[regime]['total'] += 1
            if sig['approved']:
                regime_stats[regime]['approved'] += 1
            if sig['obi']:
                regime_stats[regime]['avg_obi'].append(sig['obi'])
        
        result = {}
        for regime, stats in sorted(regime_stats.items(), key=lambda x: x[1]['total'], reverse=True):
            approval_pct = (stats['approved'] / stats['total'] * 100) if stats['total'] > 0 else 0
            avg_obi = sum(stats['avg_obi']) / len(stats['avg_obi']) if stats['avg_obi'] else 0.0
            result[regime] = {
                'total': stats['total'],
                'approved': stats['approved'],
                'approval_rate': f"{approval_pct:.1f}%",
                'avg_obi': f"{avg_obi:.2f}",
            }
        
        return result
    
    def analyze_by_asset(self):
        """Analysis signals by trading asset."""
        signals = self.get_signals(limit=10000)
        
        asset_stats = defaultdict(lambda: {'approved': 0, 'total': 0})
        
        for sig in signals:
            asset = sig['asset'] or 'Unknown'
            asset_stats[asset]['total'] += 1
            if sig['approved']:
                asset_stats[asset]['approved'] += 1
        
        result = {}
        for asset, stats in sorted(asset_stats.items(), key=lambda x: x[1]['total'], reverse=True):
            approval_pct = (stats['approved'] / stats['total'] * 100) if stats['total'] > 0 else 0
            result[asset] = {
                'total': stats['total'],
                'approved': stats['approved'],
                'approval_rate': f"{approval_pct:.1f}%"
            }
        
        return result
    
    def analyze_manipulation_impact(self):
        """Analyze how manipulation warnings affect approval."""
        signals = self.get_signals(limit=10000)
        
        with_warnings = [s for s in signals if len(s['manipulation_warnings']) > 0]
        without_warnings = [s for s in signals if len(s['manipulation_warnings']) == 0]
        
        with_approved = sum(1 for s in with_warnings if s['approved'])
        without_approved = sum(1 for s in without_warnings if s['approved'])
        
        with_rate = (with_approved / len(with_warnings) * 100) if with_warnings else 0
        without_rate = (without_approved / len(without_warnings) * 100) if without_warnings else 0
        
        # Count warning types
        warning_counts = Counter()
        for sig in with_warnings:
            for warning in sig['manipulation_warnings']:
                if isinstance(warning, str):
                    # Extract warning type
                    warning_type = warning.split(' - ')[0] if ' - ' in warning else warning[:30]
                    warning_counts[warning_type] += 1
        
        return {
            'signals_with_warnings': len(with_warnings),
            'signals_without_warnings': len(without_warnings),
            'approval_rate_with_warnings': f"{with_rate:.1f}%",
            'approval_rate_without_warnings': f"{without_rate:.1f}%",
            'warning_impact': f"{abs(without_rate - with_rate):.1f}% difference",
            'top_warnings': dict(warning_counts.most_common(5)),
        }
    
    def analyze_obi_effectiveness(self):
        """Analyze OBI value distribution and approval correlation."""
        signals = self.get_signals(limit=10000)
        
        obi_buckets = {
            'extreme_bullish': {'count': 0, 'approved': 0, 'range': (1.3, 99)},
            'extreme_bearish': {'count': 0, 'approved': 0, 'range': (0, 0.77)},
            'bullish': {'count': 0, 'approved': 0, 'range': (1.1, 1.3)},
            'bearish': {'count': 0, 'approved': 0, 'range': (0.77, 0.91)},
            'neutral': {'count': 0, 'approved': 0, 'range': (0.91, 1.1)},
        }
        
        for sig in signals:
            obi = sig.get('obi', 1.0) or 1.0
            
            # Classify OBI
            bucket = None
            if obi > 1.3:
                bucket = 'extreme_bullish'
            elif obi < 0.77:
                bucket = 'extreme_bearish'
            elif obi > 1.1:
                bucket = 'bullish'
            elif obi < 0.91:
                bucket = 'bearish'
            else:
                bucket = 'neutral'
            
            obi_buckets[bucket]['count'] += 1
            if sig['approved']:
                obi_buckets[bucket]['approved'] += 1
        
        result = {}
        for bucket, stats in obi_buckets.items():
            approval_pct = (stats['approved'] / stats['count'] * 100) if stats['count'] > 0 else 0
            result[bucket] = {
                'total': stats['count'],
                'approved': stats['approved'],
                'approval_rate': f"{approval_pct:.1f}%",
            }
        
        return result
    
    def analyze_candle_count_distribution(self):
        """Analyze reversal patterns by candle count."""
        signals = self.get_signals(limit=10000)
        
        candle_stats = defaultdict(lambda: {'approved': 0, 'total': 0})
        
        for sig in signals:
            count = sig.get('candle_count', 0)
            if count > 0:
                candle_stats[count]['total'] += 1
                if sig['approved']:
                    candle_stats[count]['approved'] += 1
        
        result = {}
        for c in sorted(candle_stats.keys()):
            stats = candle_stats[c]
            approval_pct = (stats['approved'] / stats['total'] * 100) if stats['total'] > 0 else 0
            result[f'{c}_candles'] = {
                'total': stats['total'],
                'approved': stats['approved'],
                'approval_rate': f"{approval_pct:.1f}%"
            }
        
        return result
    
    def generate_report(self):
        """Generate comprehensive analysis report."""
        print("\n" + "="*80)
        print("📊 SIGNAL HISTORY ANALYSIS REPORT")
        print("="*80)
        
        # Approval rate
        print("\n1️⃣  OVERALL SIGNAL STATISTICS")
        print("-" * 80)
        approval = self.analyze_approval_rate()
        for key, val in approval.items():
            print(f"  {key:.<30} {val}")
        
        # Rejection analysis
        print("\n2️⃣  REJECTION ANALYSIS")
        print("-" * 80)
        rejections = self.analyze_rejection_reasons()
        if 'message' not in rejections:
            print(f"  Total rejected: {rejections.get('total_rejected', 0)}")
            print("  Top rejection reasons:")
            for reason, count in list(rejections.get('rejection_reasons', {}).items())[:5]:
                print(f"    • {reason}: {count}")
        else:
            print(f"  {rejections['message']}")
        
        # Pattern analysis
        print("\n3️⃣  PATTERN EFFECTIVENESS")
        print("-" * 80)
        patterns = self.analyze_by_pattern()
        for pattern, stats in list(patterns.items())[:10]:
            print(f"  {pattern}")
            print(f"    ✓ Approved: {stats['approved']}/{stats['total']} ({stats['approval_rate']})")
        
        # Regime analysis
        print("\n4️⃣  REGIME DISTRIBUTION")
        print("-" * 80)
        regimes = self.analyze_by_regime()
        for regime, stats in regimes.items():
            print(f"  {regime}")
            print(f"    ✓ {stats['approved']}/{stats['total']} approved ({stats['approval_rate']})")
            print(f"    📊 Avg OBI: {stats['avg_obi']}")
        
        # Asset analysis
        print("\n5️⃣  BY ASSET")
        print("-" * 80)
        assets = self.analyze_by_asset()
        for asset, stats in assets.items():
            print(f"  {asset}")
            print(f"    ✓ {stats['approved']}/{stats['total']} approved ({stats['approval_rate']})")
        
        # Manipulation impact
        print("\n6️⃣  MANIPULATION WARNING IMPACT")
        print("-" * 80)
        manip = self.analyze_manipulation_impact()
        print(f"  Signals with warnings: {manip['signals_with_warnings']}")
        print(f"  Signals without warnings: {manip['signals_without_warnings']}")
        print(f"  Approval rate WITH warnings: {manip['approval_rate_with_warnings']}")
        print(f"  Approval rate WITHOUT warnings: {manip['approval_rate_without_warnings']}")
        print(f"  Impact: {manip['warning_impact']}")
        if manip.get('top_warnings'):
            print("  Top warning types:")
            for warning, count in manip['top_warnings'].items():
                print(f"    • {warning}: {count}")
        
        # OBI effectiveness
        print("\n7️⃣  OBI EFFECTIVENESS")
        print("-" * 80)
        obi = self.analyze_obi_effectiveness()
        for bucket, stats in obi.items():
            print(f"  {bucket}")
            print(f"    • {stats['approved']}/{stats['total']} approved ({stats['approval_rate']})")
        
        # Candle count
        print("\n8️⃣  CANDLE PATTERN ANALYSIS")
        print("-" * 80)
        candles = self.analyze_candle_count_distribution()
        for pattern, stats in candles.items():
            print(f"  {pattern}")
            print(f"    • {stats['approved']}/{stats['total']} approved ({stats['approval_rate']})")
        
        print("\n" + "="*80 + "\n")
    
    def export_json(self, filename: str = "signal_analysis.json"):
        """Export full analysis to JSON."""
        report = {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'days_analyzed': self.days_back,
            'cutoff_date': self.cutoff_date,
            'approval_rate': self.analyze_approval_rate(),
            'rejection_analysis': self.analyze_rejection_reasons(),
            'by_pattern': self.analyze_by_pattern(),
            'by_regime': self.analyze_by_regime(),
            'by_asset': self.analyze_by_asset(),
            'manipulation_impact': self.analyze_manipulation_impact(),
            'obi_effectiveness': self.analyze_obi_effectiveness(),
            'candle_distribution': self.analyze_candle_count_distribution(),
        }
        
        filepath = Path(__file__).parent / filename
        with open(filepath, 'w') as f:
            json.dump(report, f, indent=2)
        
        print(f"📁 Report exported to: {filepath}")
        return filepath


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Analyze trading signal history",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python signal_analyzer.py                    # Last 7 days
  python signal_analyzer.py --days 30          # Last 30 days
  python signal_analyzer.py --export           # Export to JSON
  python signal_analyzer.py --days 14 --export # 14 days + export
        """
    )
    
    parser.add_argument('--days', type=int, default=7,
                        help='Days back to analyze (default: 7)')
    parser.add_argument('--export', action='store_true',
                        help='Export results to JSON')
    parser.add_argument('--limit', type=int, default=10000,
                        help='Max signals to analyze (default: 10000)')
    
    args = parser.parse_args()
    
    try:
        analyzer = SignalAnalyzer(days_back=args.days)
        analyzer.generate_report()
        
        if args.export:
            analyzer.export_json()
    
    except Exception as e:
        logger.error(f"Analysis error: {e}")
        print(f"❌ Error: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    exit(main())
