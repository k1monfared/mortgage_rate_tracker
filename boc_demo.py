"""
Demo script for BoC Monitor
Tests the analysis function with sample Bank of Canada text
"""

from boc_monitor import BoCMonitor
import os

# Sample texts representing different monetary policy stances

HAWKISH_SAMPLE = """
Bank of Canada Increases Policy Rate by 50 Basis Points

The Bank of Canada today increased its target for the overnight rate to 2.50%, 
with the Bank Rate at 2.75% and the deposit rate at 2.50%.

Inflation in Canada is higher and more persistent than the Bank expected. 
CPI inflation is now expected to average almost 8% in the third quarter of 2022 
before declining to about 7% in the fourth quarter. The Bank's preferred measures 
of core inflation are not yet showing meaningful evidence that underlying price 
pressures are easing.

The economy continues to operate in excess demand and labour markets remain tight. 
Employment is growing strongly, with the unemployment rate at historic lows.

The Bank is committed to using its monetary policy tools to return inflation to 
the 2% target and will continue to take action as required to achieve that goal.
"""

DOVISH_SAMPLE = """
Bank of Canada Reduces Policy Rate by 50 Basis Points

The Bank of Canada today reduced its target for the overnight rate to 3.00%, 
with the Bank Rate at 3.25% and the deposit rate at 3.00%.

Inflation in Canada has continued to ease, with CPI inflation declining to 2.4% 
in the third quarter. The Bank's preferred measures of core inflation have also 
moderated, suggesting underlying price pressures are cooling.

Economic growth has been weaker than anticipated. GDP contracted in the second 
quarter, and labour market conditions have softened considerably. The unemployment 
rate has risen to 6.5%, its highest level since the pandemic.

With inflation returning sustainably to the 2% target and downside risks to growth 
increasing, the Bank judges that monetary policy no longer needs to be as restrictive. 
The Bank will continue to monitor economic developments and adjust policy as needed.
"""

NEUTRAL_SAMPLE = """
Bank of Canada Maintains Policy Rate

The Bank of Canada today held its target for the overnight rate at 4.50%, 
with the Bank Rate at 4.75% and the deposit rate at 4.50%.

Recent economic data has been mixed. Inflation has eased from its peak but remains 
above the 2% target. Core inflation measures show signs of moderating but remain 
elevated. Economic growth has been modest, with some sectors showing strength while 
others are slowing.

The labour market remains relatively firm, though there are early signs of softening. 
Wage growth continues but at a more moderate pace.

The Bank continues to assess the balance of risks. Monetary policy remains restrictive, 
but the Governing Council is carefully monitoring economic data to determine whether 
further adjustments are needed. The Bank remains data-dependent in its approach.
"""

def run_demo():
    """Run demonstration of the BoC Monitor with sample texts"""
    
    print("="*70)
    print("BANK OF CANADA MONITOR - DEMONSTRATION")
    print("="*70)
    print("\nThis demo shows how the monitor analyzes different policy stances")
    print("using sample Bank of Canada text.\n")
    
    # Get API key
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    
    if not api_key:
        print("⚠️  Warning: ANTHROPIC_API_KEY not set")
        print("Set it with: export ANTHROPIC_API_KEY='your-key-here'")
        print("\nFor this demo, you can enter it now:")
        api_key = input("Enter your Anthropic API key (or press Enter to skip): ").strip()
        
        if not api_key:
            print("\n❌ Cannot run demo without API key")
            return
    
    # Initialize monitor
    monitor = BoCMonitor(anthropic_api_key=api_key)
    
    samples = [
        ("HAWKISH SAMPLE (Rate Increase Expected)", HAWKISH_SAMPLE),
        ("DOVISH SAMPLE (Rate Cut Expected)", DOVISH_SAMPLE),
        ("NEUTRAL SAMPLE (Rate Hold Expected)", NEUTRAL_SAMPLE)
    ]
    
    analyses = []
    
    for title, sample_text in samples:
        print(f"\n{'='*70}")
        print(f"Analyzing: {title}")
        print(f"{'='*70}\n")
        
        print("Sample text preview:")
        print(sample_text[:200] + "...\n")
        
        print("🤖 Sending to Claude for analysis...")
        
        analysis = monitor.analyze_with_llm(sample_text, "Policy Announcement")
        
        if analysis.get('stance') != 'ERROR':
            print(f"\n✅ Analysis Complete!")
            print(f"   Stance: {analysis.get('stance')}")
            print(f"   Confidence: {analysis.get('confidence')}%")
            print(f"   Rate Change Probability: {analysis.get('rate_change_probability')}%")
            print(f"   Direction: {analysis.get('direction')}")
            print(f"   Inflation Concern: {analysis.get('inflation_concern')}")
            print(f"   Growth Outlook: {analysis.get('growth_outlook')}")
            
            if analysis.get('key_signals'):
                print(f"\n   Key Signals:")
                for signal in analysis['key_signals'][:3]:
                    print(f"     • {signal}")
            
            if analysis.get('summary'):
                print(f"\n   Summary: {analysis['summary']}")
            
            analyses.append(analysis)
        else:
            print(f"\n❌ Analysis failed: {analysis.get('error')}")
        
        print("\n" + "-"*70)
    
    # Generate summary report
    if analyses:
        print(f"\n{'='*70}")
        print("DEMO SUMMARY")
        print(f"{'='*70}\n")
        
        report = monitor.generate_report(analyses)
        print(report)
    
    print("\n✅ Demo complete!")
    print("\nTo monitor real Bank of Canada data, run:")
    print("   python boc_monitor.py")


if __name__ == "__main__":
    run_demo()
