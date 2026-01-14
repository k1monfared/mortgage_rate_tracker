"""
Bank of Canada Monetary Policy Monitor
Automatically fetches and analyzes BoC communications for rate change signals
"""

import requests
from bs4 import BeautifulSoup
import json
from datetime import datetime, timedelta
import anthropic
import os
from typing import Dict, List, Optional
import time

from config import OUTPUT_DIR, MAX_CHUNK_SIZE, CHUNK_OVERLAP


class DocumentChunker:
    """
    Smart document chunking that preserves sentence/paragraph boundaries
    """
    def __init__(self, chunk_size: int = MAX_CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
        """
        Initialize the chunker

        Args:
            chunk_size: Maximum characters per chunk
            overlap: Number of characters to overlap between chunks
        """
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk_text(self, text: str) -> List[tuple]:
        """
        Split text into chunks with overlap
        Attempts to break on paragraph boundaries

        Args:
            text: The text to chunk

        Returns:
            List of (chunk_text, chunk_index, total_chunks) tuples
        """
        if len(text) <= self.chunk_size:
            return [(text, 0, 1)]

        chunks = []
        paragraphs = text.split('\n\n')

        current_chunk = ""

        for para in paragraphs:
            # If adding this paragraph would exceed chunk size
            if len(current_chunk) + len(para) + 2 > self.chunk_size:
                if current_chunk:
                    # Save current chunk
                    chunks.append(current_chunk)
                    # Start new chunk with overlap from previous
                    overlap_text = current_chunk[-self.overlap:] if len(current_chunk) > self.overlap else current_chunk
                    current_chunk = overlap_text + "\n\n" + para
                else:
                    # Single paragraph exceeds chunk size - force split
                    if len(para) > self.chunk_size:
                        # Split long paragraph into sentences or by character limit
                        for i in range(0, len(para), self.chunk_size - self.overlap):
                            chunk_part = para[i:i + self.chunk_size]
                            chunks.append(chunk_part)
                        current_chunk = ""
                    else:
                        current_chunk = para
            else:
                # Add paragraph to current chunk
                if current_chunk:
                    current_chunk += "\n\n" + para
                else:
                    current_chunk = para

        # Add final chunk if exists
        if current_chunk and current_chunk not in chunks:
            chunks.append(current_chunk)

        # Return with metadata
        total = len(chunks)
        return [(chunk, idx, total) for idx, chunk in enumerate(chunks)]


class BoCMonitor:
    def __init__(self, anthropic_api_key: str):
        """
        Initialize the BoC Monitor

        Args:
            anthropic_api_key: Your Anthropic API key
        """
        if not anthropic_api_key or anthropic_api_key == 'your-api-key-here':
            raise ValueError("Valid ANTHROPIC_API_KEY required. Set the environment variable or pass a valid key.")

        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.base_url = "https://www.bankofcanada.ca"
        self.chunker = DocumentChunker()
        
    def fetch_latest_policy_announcement(self) -> Optional[Dict]:
        """Fetch the latest monetary policy announcement"""
        try:
            url = f"{self.base_url}/core/monetary-policy/key-interest-rate/"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Try to extract the latest announcement
            announcement = {
                'url': url,
                'fetched_at': datetime.now().isoformat(),
                'type': 'Policy Rate Page',
                'text': soup.get_text()  # Full text (chunking will handle long docs)
            }
            
            return announcement
            
        except Exception as e:
            print(f"Error fetching policy announcement: {e}")
            return None
    
    def fetch_press_releases(self, days_back: int = 30) -> List[Dict]:
        """Fetch recent press releases from BoC"""
        try:
            url = f"{self.base_url}/news/"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            releases = []
            
            # Look for press release links
            for link in soup.find_all('a', href=True):
                if 'press-release' in link['href'] or 'statement' in link['href']:
                    release_url = link['href']
                    if not release_url.startswith('http'):
                        release_url = self.base_url + release_url
                    
                    releases.append({
                        'title': link.get_text().strip(),
                        'url': release_url,
                        'type': 'Press Release'
                    })
                    
                    if len(releases) >= 5:  # Limit to 5 most recent
                        break
            
            return releases
            
        except Exception as e:
            print(f"Error fetching press releases: {e}")
            return []
    
    def fetch_monetary_policy_report(self) -> Optional[Dict]:
        """Fetch information about the latest Monetary Policy Report"""
        try:
            url = f"{self.base_url}/publications/mpr/"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            return {
                'url': url,
                'type': 'Monetary Policy Report',
                'text': soup.get_text()  # Full text (chunking will handle long docs)
            }
            
        except Exception as e:
            print(f"Error fetching MPR: {e}")
            return None
    
    def fetch_speeches(self) -> List[Dict]:
        """Fetch recent speeches by BoC officials"""
        try:
            url = f"{self.base_url}/news/speeches-and-webcasts/"
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            speeches = []
            
            for link in soup.find_all('a', href=True):
                if '/speech' in link['href']:
                    speech_url = link['href']
                    if not speech_url.startswith('http'):
                        speech_url = self.base_url + speech_url
                    
                    speeches.append({
                        'title': link.get_text().strip(),
                        'url': speech_url,
                        'type': 'Speech'
                    })
                    
                    if len(speeches) >= 3:  # Limit to 3 most recent
                        break
            
            return speeches
            
        except Exception as e:
            print(f"Error fetching speeches: {e}")
            return []
    
    def _analyze_single_chunk(self, content: str, source_type: str) -> Dict:
        """
        Use Claude to analyze the monetary policy stance for a single chunk

        Args:
            content: The text content to analyze
            source_type: Type of content (e.g., 'Press Release', 'Speech')

        Returns:
            Analysis results including stance, confidence, and reasoning
        """
        
        analysis_prompt = f"""You are a monetary policy analyst. Analyze the following {source_type} from the Bank of Canada and provide a structured assessment.

Content to analyze:
{content}

Please analyze this content and provide:

1. **Policy Stance** (choose one):
   - HAWKISH (signals rate increases likely)
   - DOVISH (signals rate cuts likely)
   - NEUTRAL (balanced, no clear direction)
   - HOLD (maintaining current rates)

2. **Confidence Level** (0-100): How confident are you in this assessment?

3. **Rate Change Probability** (0-100): What's the probability of a rate change in the next 3 months?

4. **Direction**: If rate change likely, will it be UP or DOWN?

5. **Key Signals**: List 3-5 specific phrases or indicators that support your assessment

6. **Summary**: A brief 2-3 sentence summary of the monetary policy implications

7. **Inflation Concerns** (HIGH/MEDIUM/LOW): Level of concern about inflation

8. **Economic Growth Outlook** (STRONG/MODERATE/WEAK): How they view economic growth

Please respond in JSON format with these exact keys:
{{
  "stance": "HAWKISH|DOVISH|NEUTRAL|HOLD",
  "confidence": 85,
  "rate_change_probability": 60,
  "direction": "UP|DOWN|NONE",
  "key_signals": ["signal 1", "signal 2", ...],
  "summary": "Brief summary here",
  "inflation_concern": "HIGH|MEDIUM|LOW",
  "growth_outlook": "STRONG|MODERATE|WEAK"
}}
"""
        
        try:
            message = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                temperature=0.3,  # Lower temperature for more consistent analysis
                messages=[
                    {"role": "user", "content": analysis_prompt}
                ]
            )
            
            response_text = message.content[0].text
            
            # Try to extract JSON from the response
            # Sometimes Claude wraps JSON in markdown code blocks
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            
            analysis = json.loads(response_text)
            analysis['analyzed_at'] = datetime.now().isoformat()
            analysis['source_type'] = source_type
            
            return analysis
            
        except json.JSONDecodeError as e:
            print(f"Error parsing LLM response: {e}")
            print(f"Response was: {response_text}")
            return {
                'stance': 'UNKNOWN',
                'confidence': 0,
                'error': f'Failed to parse response: {str(e)}',
                'raw_response': response_text
            }
        except Exception as e:
            print(f"Error in LLM analysis: {e}")
            return {
                'stance': 'ERROR',
                'confidence': 0,
                'error': str(e)
            }

    def analyze_with_llm(self, content: str, source_type: str) -> Dict:
        """
        Analyze content with automatic chunking for long documents

        Args:
            content: The text content to analyze
            source_type: Type of content (e.g., 'Press Release', 'Speech')

        Returns:
            Analysis results including stance, confidence, and reasoning
        """
        # Route to appropriate method based on content length
        if len(content) > MAX_CHUNK_SIZE:
            return self.analyze_with_llm_chunked(content, source_type)
        else:
            return self._analyze_single_chunk(content, source_type)

    def analyze_with_llm_chunked(self, content: str, source_type: str) -> Dict:
        """
        Analyze long content using map-reduce pattern with chunking

        Args:
            content: The text content to analyze
            source_type: Type of content

        Returns:
            Synthesized analysis results
        """
        print(f"   Document length: {len(content)} chars - using chunked analysis")

        # Split into chunks
        chunks = self.chunker.chunk_text(content)
        print(f"   Split into {len(chunks)} chunks")

        chunk_analyses = []

        # Map phase: analyze each chunk
        for chunk_text, idx, total in chunks:
            print(f"   Analyzing chunk {idx+1}/{total}...")

            chunk_prompt = f"""You are analyzing PART {idx+1} of {total} of a {source_type} from the Bank of Canada.

Content to analyze (Part {idx+1}/{total}):
{chunk_text}

Provide a focused analysis of THIS SECTION ONLY. Focus on the specific information present in this section.

Analyze and provide:
1. Key policy signals in THIS section
2. Inflation concerns mentioned HERE
3. Economic outlook discussed HERE
4. Any rate change indicators in THIS section

Respond in JSON format:
{{
  "chunk_index": {idx},
  "key_signals": ["signal 1", "signal 2", ...],
  "inflation_concern": "HIGH|MEDIUM|LOW|UNCLEAR",
  "growth_outlook": "STRONG|MODERATE|WEAK|UNCLEAR",
  "summary": "Brief summary of this section's key points"
}}
"""

            try:
                message = self.client.messages.create(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1000,
                    temperature=0.3,
                    messages=[{"role": "user", "content": chunk_prompt}]
                )

                response_text = message.content[0].text

                # Parse JSON response
                if "```json" in response_text:
                    json_start = response_text.find("```json") + 7
                    json_end = response_text.find("```", json_start)
                    response_text = response_text[json_start:json_end].strip()
                elif "```" in response_text:
                    json_start = response_text.find("```") + 3
                    json_end = response_text.find("```", json_start)
                    response_text = response_text[json_start:json_end].strip()

                chunk_analysis = json.loads(response_text)
                chunk_analyses.append(chunk_analysis)

                time.sleep(1)  # Rate limiting

            except Exception as e:
                print(f"   ⚠️  Error analyzing chunk {idx+1}: {e}")
                continue

        if not chunk_analyses:
            return {
                'stance': 'ERROR',
                'confidence': 0,
                'error': 'All chunk analyses failed'
            }

        # Reduce phase: synthesize all chunk analyses
        print(f"   Synthesizing {len(chunk_analyses)} chunk analyses...")
        return self._synthesize_chunk_analyses(chunk_analyses, source_type)

    def _synthesize_chunk_analyses(self, chunk_analyses: List[Dict], source_type: str) -> Dict:
        """
        Synthesize multiple chunk analyses into single coherent analysis

        Args:
            chunk_analyses: List of chunk analysis results
            source_type: Type of content

        Returns:
            Final synthesized analysis
        """
        # Aggregate all key signals
        all_signals = []
        for ca in chunk_analyses:
            all_signals.extend(ca.get('key_signals', []))

        # Combine summaries
        combined_summaries = [ca.get('summary', '') for ca in chunk_analyses if ca.get('summary')]

        # Create synthesis prompt
        synthesis_prompt = f"""You analyzed a {source_type} from the Bank of Canada in {len(chunk_analyses)} parts.
Here are the analyses from each part:

{json.dumps(chunk_analyses, indent=2)}

Now provide a FINAL OVERALL analysis synthesizing all parts. Consider:
- What is the overall policy stance across the entire document?
- What are the most important signals? (Pick top 5, don't list everything)
- What's the probability of rate change in next 3 months?
- Overall inflation concern level?
- Overall growth outlook?

Respond in JSON format with these exact keys:
{{
  "stance": "HAWKISH|DOVISH|NEUTRAL|HOLD",
  "confidence": 85,
  "rate_change_probability": 60,
  "direction": "UP|DOWN|NONE",
  "key_signals": ["top signal 1", "top signal 2", ...],
  "summary": "2-3 sentence synthesis of the overall message",
  "inflation_concern": "HIGH|MEDIUM|LOW",
  "growth_outlook": "STRONG|MODERATE|WEAK"
}}
"""

        try:
            message = self.client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                temperature=0.3,
                messages=[{"role": "user", "content": synthesis_prompt}]
            )

            response_text = message.content[0].text

            # Parse JSON
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            final_analysis = json.loads(response_text)
            final_analysis['analyzed_at'] = datetime.now().isoformat()
            final_analysis['source_type'] = source_type
            final_analysis['chunks_analyzed'] = len(chunk_analyses)

            return final_analysis

        except Exception as e:
            print(f"   ⚠️  Error in synthesis: {e}")
            # Fallback: use first chunk's analysis as base
            if chunk_analyses:
                fallback = {
                    'stance': 'NEUTRAL',
                    'confidence': 50,
                    'rate_change_probability': 25,
                    'direction': 'NONE',
                    'key_signals': all_signals[:5],
                    'summary': ' '.join(combined_summaries[:2]),
                    'inflation_concern': chunk_analyses[0].get('inflation_concern', 'UNCLEAR'),
                    'growth_outlook': chunk_analyses[0].get('growth_outlook', 'UNCLEAR'),
                    'analyzed_at': datetime.now().isoformat(),
                    'source_type': source_type,
                    'synthesis_error': str(e)
                }
                return fallback
            else:
                return {
                    'stance': 'ERROR',
                    'confidence': 0,
                    'error': str(e)
                }

    def fetch_detailed_content(self, url: str) -> Optional[str]:
        """Fetch the full content from a URL"""
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Remove script and style elements
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            
            # Get text
            text = soup.get_text()
            
            # Clean up whitespace
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = '\n'.join(chunk for chunk in chunks if chunk)

            # Return full text (chunking will handle long documents)
            return text
            
        except Exception as e:
            print(f"Error fetching content from {url}: {e}")
            return None
    
    def generate_report(self, analyses: List[Dict]) -> str:
        """Generate a comprehensive report from multiple analyses"""
        
        if not analyses:
            return "No analyses available"
        
        # Calculate aggregate metrics
        total_confidence = sum(a.get('confidence', 0) for a in analyses)
        avg_confidence = total_confidence / len(analyses) if analyses else 0
        
        hawkish_count = sum(1 for a in analyses if a.get('stance') == 'HAWKISH')
        dovish_count = sum(1 for a in analyses if a.get('stance') == 'DOVISH')
        neutral_count = sum(1 for a in analyses if a.get('stance') == 'NEUTRAL')
        hold_count = sum(1 for a in analyses if a.get('stance') == 'HOLD')
        
        avg_rate_change_prob = sum(a.get('rate_change_probability', 0) for a in analyses) / len(analyses)
        
        # Aggregate key signals
        all_signals = []
        for a in analyses:
            all_signals.extend(a.get('key_signals', []))
        
        report = f"""
╔═══════════════════════════════════════════════════════════════════╗
║       BANK OF CANADA MONETARY POLICY MONITORING REPORT          ║
║                Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                    ║
╚═══════════════════════════════════════════════════════════════════╝

📊 AGGREGATE ANALYSIS (Based on {len(analyses)} sources)
{'='*70}
Average Confidence Level: {avg_confidence:.1f}%
Average Rate Change Probability (3 months): {avg_rate_change_prob:.1f}%

Stance Distribution:
  🔴 HAWKISH (Rate increases likely):  {hawkish_count} source(s)
  🟢 DOVISH (Rate cuts likely):        {dovish_count} source(s)
  🟡 NEUTRAL (No clear direction):     {neutral_count} source(s)
  🔵 HOLD (Maintaining current rate):  {hold_count} source(s)

Overall Assessment: """
        
        if hawkish_count > dovish_count and hawkish_count > hold_count:
            report += "⚠️  HAWKISH BIAS - Rate increases more likely\n"
        elif dovish_count > hawkish_count and dovish_count > hold_count:
            report += "📉 DOVISH BIAS - Rate cuts more likely\n"
        elif hold_count > hawkish_count and hold_count > dovish_count:
            report += "⏸️  HOLD STANCE - Rates likely to remain stable\n"
        else:
            report += "⚖️  MIXED SIGNALS - No clear consensus\n"
        
        report += f"\n{'='*70}\n"
        report += "\n📋 INDIVIDUAL SOURCE ANALYSES\n"
        report += f"{'='*70}\n\n"
        
        for i, analysis in enumerate(analyses, 1):
            stance_emoji = {
                'HAWKISH': '🔴',
                'DOVISH': '🟢',
                'NEUTRAL': '🟡',
                'HOLD': '🔵',
                'UNKNOWN': '❓',
                'ERROR': '❌'
            }.get(analysis.get('stance', 'UNKNOWN'), '❓')
            
            report += f"{i}. {stance_emoji} {analysis.get('source_type', 'Unknown Source')}\n"
            report += f"   Stance: {analysis.get('stance', 'UNKNOWN')}\n"
            report += f"   Confidence: {analysis.get('confidence', 0)}%\n"
            report += f"   Rate Change Probability: {analysis.get('rate_change_probability', 0)}%\n"
            report += f"   Direction: {analysis.get('direction', 'NONE')}\n"
            report += f"   Inflation Concern: {analysis.get('inflation_concern', 'N/A')}\n"
            report += f"   Growth Outlook: {analysis.get('growth_outlook', 'N/A')}\n"
            
            if analysis.get('summary'):
                report += f"   Summary: {analysis['summary']}\n"
            
            if analysis.get('key_signals'):
                report += f"   Key Signals:\n"
                for signal in analysis['key_signals'][:3]:  # Show top 3
                    report += f"     • {signal}\n"
            
            report += "\n"
        
        report += f"{'='*70}\n"
        report += "\n🎯 ACTIONABLE INSIGHTS\n"
        report += f"{'='*70}\n"
        
        if avg_rate_change_prob > 60:
            report += "⚠️  HIGH probability of rate change in next 3 months\n"
            report += "   → Monitor BoC announcements closely\n"
            report += "   → Consider locking in rates if borrowing soon\n"
        elif avg_rate_change_prob > 30:
            report += "⚡ MODERATE probability of rate change\n"
            report += "   → Stay informed of economic indicators\n"
            report += "   → Review mortgage renewal options\n"
        else:
            report += "✅ LOW probability of rate change\n"
            report += "   → Stable rate environment expected\n"
            report += "   → Variable rate products may be attractive\n"
        
        report += f"\n{'='*70}\n"
        
        return report
    
    def run_full_analysis(self) -> str:
        """Run complete monitoring and analysis"""
        print("🔍 Starting Bank of Canada Monitoring Analysis...\n")
        
        analyses = []
        
        # 1. Fetch and analyze policy announcement
        print("📄 Fetching policy rate announcement...")
        policy = self.fetch_latest_policy_announcement()
        if policy and policy.get('text'):
            print("   ✓ Analyzing with LLM...")
            analysis = self.analyze_with_llm(policy['text'], policy['type'])
            analyses.append(analysis)
            time.sleep(1)  # Rate limiting
        
        # 2. Fetch and analyze press releases
        print("\n📰 Fetching recent press releases...")
        releases = self.fetch_press_releases()
        for release in releases[:2]:  # Analyze top 2
            print(f"   Fetching: {release['title'][:60]}...")
            content = self.fetch_detailed_content(release['url'])
            if content:
                print("   ✓ Analyzing with LLM...")
                analysis = self.analyze_with_llm(content, release['type'])
                analysis['title'] = release['title']
                analyses.append(analysis)
                time.sleep(1)  # Rate limiting
        
        # 3. Fetch and analyze MPR
        print("\n📊 Fetching Monetary Policy Report...")
        mpr = self.fetch_monetary_policy_report()
        if mpr and mpr.get('text'):
            print("   ✓ Analyzing with LLM...")
            analysis = self.analyze_with_llm(mpr['text'], mpr['type'])
            analyses.append(analysis)
            time.sleep(1)  # Rate limiting
        
        print("\n" + "="*70)
        print("✅ Analysis Complete!\n")
        
        # Generate report
        report = self.generate_report(analyses)
        
        # Save to file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = OUTPUT_DIR / f'boc_analysis_{timestamp}.txt'
        with open(filename, 'w') as f:
            f.write(report)

        print(f"💾 Report saved to: {filename}\n")

        # Also save raw data as JSON
        json_filename = OUTPUT_DIR / f'boc_analysis_{timestamp}.json'
        with open(json_filename, 'w') as f:
            json.dump({
                'timestamp': timestamp,
                'analyses': analyses
            }, f, indent=2)

        print(f"💾 Raw data saved to: {json_filename}\n")
        
        return report


def main():
    """Enhanced main execution function with subcommands"""
    import argparse

    parser = argparse.ArgumentParser(
        description='Bank of Canada Monetary Policy Monitor',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run policy analysis (default)
  python boc_monitor.py analyze

  # Initialize historical data (100 years)
  python boc_monitor.py update-rates --full

  # Update incremental data
  python boc_monitor.py update-rates

  # Generate plots
  python boc_monitor.py plot --type both --output rates.html
  python boc_monitor.py plot --type policy
  python boc_monitor.py plot --type prime
        """
    )

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Analyze command (default behavior)
    analyze_parser = subparsers.add_parser('analyze', help='Run policy analysis')

    # Update rates command
    update_parser = subparsers.add_parser('update-rates', help='Update historical rate data')
    update_parser.add_argument('--full', action='store_true',
                              help='Full historical fetch (100 years)')

    # Plot command
    plot_parser = subparsers.add_parser('plot', help='Generate rate plots')
    plot_parser.add_argument('--type', choices=['policy', 'prime', 'both'],
                            default='both', help='Type of rate to plot')
    plot_parser.add_argument('--output', default='rates_plot.html',
                            help='Output HTML filename')

    args = parser.parse_args()

    # Default to analyze if no command specified
    if args.command is None:
        args.command = 'analyze'

    # Handle commands
    if args.command == 'analyze':
        # Get API key
        api_key = os.environ.get('ANTHROPIC_API_KEY')

        if not api_key:
            print("⚠️  ANTHROPIC_API_KEY environment variable not set")
            print("Please set it with: export ANTHROPIC_API_KEY='your-key-here'")
            return

        # Initialize monitor and run analysis
        try:
            monitor = BoCMonitor(anthropic_api_key=api_key)
            report = monitor.run_full_analysis()
            print(report)

        except Exception as e:
            print(f"\n❌ Error running analysis: {e}")
            import traceback
            traceback.print_exc()

    elif args.command == 'update-rates':
        from historical_rates import HistoricalRateFetcher

        fetcher = HistoricalRateFetcher()

        if args.full:
            print("Fetching 100 years of historical data...")
            fetcher.initialize_historical_data()
        else:
            print("Updating incremental data...")
            fetcher.update_incremental('policy')
            fetcher.update_incremental('prime')

        print("\n✅ Rate data updated successfully!")

        # Show metadata
        metadata = fetcher.get_metadata()
        if metadata:
            print("\nMetadata:")
            print("-" * 70)
            for key, value in metadata.items():
                print(f"  {key}: {value}")

    elif args.command == 'plot':
        from historical_rates import HistoricalRateFetcher
        from rate_plotter import RatePlotter

        fetcher = HistoricalRateFetcher()
        plotter = RatePlotter()

        if args.type == 'policy':
            data = fetcher.load_rate_data('policy')
            if data is not None:
                fig = plotter.plot_single_rate(data, 'BoC Policy Rate',
                                              'Bank of Canada Policy Rate History')
                plotter.save_plot(fig, args.output)
                print(f"\n✅ Plot saved to output/{args.output}")
            else:
                print("❌ No policy rate data found. Run 'python boc_monitor.py update-rates --full' first.")

        elif args.type == 'prime':
            data = fetcher.load_rate_data('prime')
            if data is not None:
                fig = plotter.plot_single_rate(data, 'Commercial Prime Rate',
                                              'Commercial Prime Rate History')
                plotter.save_plot(fig, args.output)
                print(f"\n✅ Plot saved to output/{args.output}")
            else:
                print("❌ No prime rate data found. Run 'python boc_monitor.py update-rates --full' first.")

        else:  # both
            policy_data = fetcher.load_rate_data('policy')
            prime_data = fetcher.load_rate_data('prime')

            if policy_data is not None and prime_data is not None:
                fig = plotter.plot_dual_rates(policy_data, prime_data)
                plotter.save_plot(fig, args.output)
                print(f"\n✅ Plot saved to output/{args.output}")
            else:
                print("❌ Missing rate data. Run 'python boc_monitor.py update-rates --full' first.")


if __name__ == "__main__":
    main()
