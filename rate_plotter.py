"""
Rate Plotter for visualizing historical interest rate data
Uses Plotly for interactive charts
"""

import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import pandas as pd
from pathlib import Path
from typing import Optional, List

from config import OUTPUT_DIR


class RatePlotter:
    """
    Create interactive visualizations of interest rate data using Plotly
    """

    def __init__(self, output_dir: Optional[Path] = None):
        """
        Initialize the plotter

        Args:
            output_dir: Directory to save plot HTML files (default: OUTPUT_DIR from config)
        """
        self.output_dir = output_dir or OUTPUT_DIR

    def plot_single_rate(self, df: pd.DataFrame, rate_name: str,
                        title: Optional[str] = None) -> go.Figure:
        """
        Plot a single rate series over time

        Args:
            df: DataFrame with 'date' and 'rate' columns
            rate_name: Name of the rate for legend and labels
            title: Chart title (optional)

        Returns:
            plotly Figure object
        """
        if df is None or len(df) == 0:
            print("Error: Empty or None DataFrame provided")
            return None

        fig = go.Figure()

        fig.add_trace(go.Scatter(
            x=df['date'],
            y=df['rate'],
            mode='lines',
            name=rate_name,
            line=dict(color='#1f77b4', width=2, shape='hv'),  # Step function
            hovertemplate='<b>Date:</b> %{x|%Y-%m-%d}<br>' +
                         '<b>Rate:</b> %{y:.2f}%<br>' +
                         '<extra></extra>'
        ))

        fig.update_layout(
            title=title or f"{rate_name} Over Time",
            xaxis_title="Date",
            yaxis_title="Rate (%)",
            hovermode='x unified',
            template='plotly_white',
            font=dict(size=12),
            title_font=dict(size=16, color='#333'),
            xaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc'
            ),
            yaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc',
                ticksuffix='%'
            ),
            plot_bgcolor='white',
            height=500
        )

        return fig

    def plot_dual_rates(self, policy_df: pd.DataFrame,
                       prime_df: pd.DataFrame,
                       title: Optional[str] = None) -> go.Figure:
        """
        Plot both policy and prime rates on the same chart

        Args:
            policy_df: DataFrame with policy rate data (date, rate columns)
            prime_df: DataFrame with prime rate data (date, rate columns)
            title: Chart title (optional)

        Returns:
            plotly Figure object
        """
        if policy_df is None or prime_df is None:
            print("Error: One or both DataFrames are None")
            return None

        fig = go.Figure()

        # Add policy rate
        fig.add_trace(go.Scatter(
            x=policy_df['date'],
            y=policy_df['rate'],
            name='BoC Policy Rate',
            line=dict(color='#2E86AB', width=2, shape='hv'),  # Step function
            hovertemplate='<b>Policy Rate</b><br>' +
                         'Date: %{x|%Y-%m-%d}<br>' +
                         'Rate: %{y:.2f}%<br>' +
                         '<extra></extra>'
        ))

        # Add prime rate
        fig.add_trace(go.Scatter(
            x=prime_df['date'],
            y=prime_df['rate'],
            name='Commercial Prime Rate',
            line=dict(color='#A23B72', width=2, shape='hv'),  # Step function
            hovertemplate='<b>Prime Rate</b><br>' +
                         'Date: %{x|%Y-%m-%d}<br>' +
                         'Rate: %{y:.2f}%<br>' +
                         '<extra></extra>'
        ))

        fig.update_layout(
            title=title or 'Bank of Canada Policy Rate vs Commercial Prime Rate',
            xaxis_title='Date',
            yaxis_title='Rate (%)',
            hovermode='x unified',
            template='plotly_white',
            font=dict(size=12),
            title_font=dict(size=16, color='#333'),
            legend=dict(
                x=0.01,
                y=0.99,
                bgcolor='rgba(255, 255, 255, 0.8)',
                bordercolor='#ccc',
                borderwidth=1
            ),
            xaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc'
            ),
            yaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc',
                ticksuffix='%'
            ),
            plot_bgcolor='white',
            height=600
        )

        return fig

    def plot_rate_changes(self, df: pd.DataFrame, rate_column: str = 'rate',
                         title: Optional[str] = None) -> go.Figure:
        """
        Plot rate changes (deltas) over time
        Highlights increases vs decreases with different colors

        Args:
            df: DataFrame with date and rate columns
            rate_column: Name of the rate column (default: 'rate')
            title: Chart title (optional)

        Returns:
            plotly Figure object
        """
        if df is None or len(df) == 0:
            print("Error: Empty or None DataFrame provided")
            return None

        # Calculate changes
        df_copy = df.copy()
        df_copy = df_copy.sort_values('date')
        df_copy['change'] = df_copy[rate_column].diff()

        # Categorize changes
        df_copy['direction'] = df_copy['change'].apply(
            lambda x: 'Increase' if x > 0 else ('Decrease' if x < 0 else 'No Change')
        )

        # Remove the first row (NaN change)
        df_copy = df_copy.dropna(subset=['change'])

        fig = go.Figure()

        # Add increases
        increases = df_copy[df_copy['direction'] == 'Increase']
        if len(increases) > 0:
            fig.add_trace(go.Bar(
                x=increases['date'],
                y=increases['change'],
                name='Rate Increase',
                marker_color='#E63946',
                hovertemplate='<b>Rate Increase</b><br>' +
                             'Date: %{x|%Y-%m-%d}<br>' +
                             'Change: +%{y:.2f}%<br>' +
                             '<extra></extra>'
            ))

        # Add decreases
        decreases = df_copy[df_copy['direction'] == 'Decrease']
        if len(decreases) > 0:
            fig.add_trace(go.Bar(
                x=decreases['date'],
                y=decreases['change'],
                name='Rate Decrease',
                marker_color='#06A77D',
                hovertemplate='<b>Rate Decrease</b><br>' +
                             'Date: %{x|%Y-%m-%d}<br>' +
                             'Change: %{y:.2f}%<br>' +
                             '<extra></extra>'
            ))

        fig.update_layout(
            title=title or 'Interest Rate Changes Over Time',
            xaxis_title='Date',
            yaxis_title='Change in Rate (percentage points)',
            barmode='relative',
            template='plotly_white',
            font=dict(size=12),
            title_font=dict(size=16, color='#333'),
            legend=dict(
                x=0.01,
                y=0.99,
                bgcolor='rgba(255, 255, 255, 0.8)',
                bordercolor='#ccc',
                borderwidth=1
            ),
            xaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc'
            ),
            yaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc',
                ticksuffix='%',
                zeroline=True,
                zerolinewidth=2,
                zerolinecolor='#333'
            ),
            plot_bgcolor='white',
            height=500
        )

        return fig

    def plot_rolling_stats(self, df: pd.DataFrame, rate_column: str = 'rate',
                          windows: Optional[List[int]] = None,
                          title: Optional[str] = None) -> go.Figure:
        """
        Plot rolling averages for different time windows

        Args:
            df: DataFrame with date and rate columns
            rate_column: Name of the rate column (default: 'rate')
            windows: List of window sizes in days (default: [30, 90, 365])
            title: Chart title (optional)

        Returns:
            plotly Figure object
        """
        if df is None or len(df) == 0:
            print("Error: Empty or None DataFrame provided")
            return None

        if windows is None:
            windows = [30, 90, 365]

        df_copy = df.copy()
        df_copy = df_copy.sort_values('date')

        fig = go.Figure()

        # Add actual rate (step function)
        fig.add_trace(go.Scatter(
            x=df_copy['date'],
            y=df_copy[rate_column],
            name='Actual Rate',
            line=dict(color='#666', width=1, dash='dot', shape='hv'),  # Step function
            opacity=0.5,
            hovertemplate='<b>Actual</b><br>' +
                         'Date: %{x|%Y-%m-%d}<br>' +
                         'Rate: %{y:.2f}%<br>' +
                         '<extra></extra>'
        ))

        # Color palette for different windows
        colors = ['#2E86AB', '#A23B72', '#F18F01']

        # Add rolling averages
        for i, window in enumerate(windows):
            rolling_avg = df_copy[rate_column].rolling(window=window, min_periods=1).mean()
            window_label = f"{window} Day Average"

            fig.add_trace(go.Scatter(
                x=df_copy['date'],
                y=rolling_avg,
                name=window_label,
                line=dict(color=colors[i % len(colors)], width=2),
                hovertemplate=f'<b>{window_label}</b><br>' +
                             'Date: %{x|%Y-%m-%d}<br>' +
                             'Rate: %{y:.2f}%<br>' +
                             '<extra></extra>'
            ))

        fig.update_layout(
            title=title or 'Interest Rate with Rolling Averages',
            xaxis_title='Date',
            yaxis_title='Rate (%)',
            hovermode='x unified',
            template='plotly_white',
            font=dict(size=12),
            title_font=dict(size=16, color='#333'),
            legend=dict(
                x=0.01,
                y=0.99,
                bgcolor='rgba(255, 255, 255, 0.8)',
                bordercolor='#ccc',
                borderwidth=1
            ),
            xaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc'
            ),
            yaxis=dict(
                showgrid=True,
                gridcolor='#f0f0f0',
                showline=True,
                linewidth=1,
                linecolor='#ccc',
                ticksuffix='%'
            ),
            plot_bgcolor='white',
            height=600
        )

        return fig

    def save_plot(self, fig: go.Figure, filename: str) -> Path:
        """
        Save plotly figure to HTML file

        Args:
            fig: plotly Figure object
            filename: Filename for the HTML file

        Returns:
            Path to the saved file
        """
        if fig is None:
            print("Error: Cannot save None figure")
            return None

        # Ensure filename has .html extension
        if not filename.endswith('.html'):
            filename += '.html'

        output_path = self.output_dir / filename

        try:
            fig.write_html(str(output_path))
            print(f"✓ Plot saved to: {output_path}")
            return output_path
        except Exception as e:
            print(f"Error saving plot: {e}")
            return None

    def show_plot(self, fig: go.Figure):
        """
        Display plot in browser

        Args:
            fig: plotly Figure object
        """
        if fig is None:
            print("Error: Cannot show None figure")
            return

        try:
            fig.show()
        except Exception as e:
            print(f"Error displaying plot: {e}")


def main():
    """Test the rate plotter with sample data"""
    import pandas as pd
    from datetime import datetime, timedelta

    print("Testing Rate Plotter")
    print("="*70)

    # Create sample data
    dates = pd.date_range(start='2020-01-01', end='2024-01-01', freq='D')
    import numpy as np

    # Simulate rate data with some variation
    np.random.seed(42)
    base_rate = 2.5
    rates = base_rate + np.cumsum(np.random.randn(len(dates)) * 0.02)
    rates = np.clip(rates, 0.5, 5.0)  # Keep rates in reasonable range

    policy_df = pd.DataFrame({
        'date': dates,
        'rate': rates
    })

    prime_df = pd.DataFrame({
        'date': dates,
        'rate': rates + 2.0  # Prime is typically ~2% higher
    })

    # Initialize plotter
    plotter = RatePlotter()

    # Test single rate plot
    print("\n1. Creating single rate plot...")
    fig1 = plotter.plot_single_rate(policy_df, 'Policy Rate', 'Test: BoC Policy Rate')
    plotter.save_plot(fig1, 'test_single_rate.html')

    # Test dual rate plot
    print("\n2. Creating dual rate plot...")
    fig2 = plotter.plot_dual_rates(policy_df, prime_df)
    plotter.save_plot(fig2, 'test_dual_rates.html')

    # Test rate changes plot
    print("\n3. Creating rate changes plot...")
    fig3 = plotter.plot_rate_changes(policy_df)
    plotter.save_plot(fig3, 'test_rate_changes.html')

    # Test rolling stats plot
    print("\n4. Creating rolling stats plot...")
    fig4 = plotter.plot_rolling_stats(policy_df, windows=[30, 90, 180])
    plotter.save_plot(fig4, 'test_rolling_stats.html')

    print("\n✓ All test plots created successfully!")


if __name__ == "__main__":
    main()
