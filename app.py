import streamlit as st
import pandas as pd
import plotly.graph_objects as go

# 1. Page Configuration
st.set_page_config(page_title="IMC Prosperity Viewer", layout="wide", page_icon="📈")

@st.cache_data
def load_data(file):
    # Load data and cache it
    return pd.read_csv(file, sep=';')

def render_product_dashboard(product_df, product_name, trades_df=None):
    """Helper function to render metrics and a chart for a specific product."""
    if product_df.empty:
        st.warning(f"No price data found for {product_name} in this file.")
        return

    st.subheader(f"Data for {product_name}")

    # Metrics Summary
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Starting Mid Price", f"{product_df['mid_price'].iloc[0]:.2f}")
    col2.metric("Ending Mid Price", f"{product_df['mid_price'].iloc[-1]:.2f}")
    col3.metric("Max Price", f"{product_df['mid_price'].max():.2f}")
    col4.metric("Min Price", f"{product_df['mid_price'].min():.2f}")

    # Interactive Plotly Chart
    fig = go.Figure()

    # Ask Line
    fig.add_trace(go.Scatter(
        x=product_df['timestamp'], y=product_df['ask_price_1'],
        mode='lines', name='Ask Price 1',
        line=dict(color='#ff4b4b', width=1), opacity=0.7
    ))

    # Mid Line
    fig.add_trace(go.Scatter(
        x=product_df['timestamp'], y=product_df['mid_price'],
        mode='lines', name='Mid Price',
        line=dict(color='#ffffff', width=2)
    ))

    # Bid Line
    fig.add_trace(go.Scatter(
        x=product_df['timestamp'], y=product_df['bid_price_1'],
        mode='lines', name='Bid Price 1',
        line=dict(color='#00c853', width=1), opacity=0.7
    ))

    # Add Trades if available
    if trades_df is not None and not trades_df.empty:
        # The trades file uses 'symbol' instead of 'product'
        product_trades = trades_df[trades_df['symbol'] == product_name]
        
        if not product_trades.empty:
            # Scale marker sizes slightly based on quantity for visual depth
            min_q = product_trades['quantity'].min()
            max_q = product_trades['quantity'].max()
            
            fig.add_trace(go.Scatter(
                x=product_trades['timestamp'], 
                y=product_trades['price'],
                mode='markers', 
                name='Executed Trades',
                marker=dict(
                    color='#00d2ff',
                    size=8,
                    line=dict(width=1, color='DarkSlateGrey')
                ),
                # Add hover text to show exactly how much was traded
                text=product_trades['quantity'].apply(lambda x: f"Quantity: {x}"),
                hoverinfo="x+y+text"
            ))

    # Chart formatting
    fig.update_layout(
        xaxis_title="Timestamp",
        yaxis_title="Price",
        template="plotly_dark",
        hovermode="x unified",
        legend=dict(yanchor="top", y=0.99, xanchor="left", x=0.01),
        margin=dict(l=0, r=0, t=10, b=0)
    )

    st.plotly_chart(fig, use_container_width=True)
    
    # Expanders for raw data
    col_exp1, col_exp2 = st.columns(2)
    with col_exp1:
        with st.expander(f"View Raw Price Data: {product_name}"):
            st.dataframe(product_df, use_container_width=True)
            
    with col_exp2:
        if trades_df is not None and not trades_df.empty:
            with st.expander(f"View Raw Trade Data: {product_name}"):
                st.dataframe(product_trades, use_container_width=True)


# Main App Execution
st.title("📈 IMC Prosperity: Dual Commodity & Trade Viewer")
st.markdown("Upload your Prices and Trades CSV files to simultaneously view **ASH_COATED_OSMIUM** and **INTARIAN_PEPPER_ROOT**.")

col_up1, col_up2 = st.columns(2)
with col_up1:
    prices_file = st.file_uploader("Upload Prices CSV (Required)", type=["csv"])
with col_up2:
    trades_file = st.file_uploader("Upload Trades CSV (Optional)", type=["csv"])

if prices_file is not None:
    df_prices = load_data(prices_file)
    df_trades = load_data(trades_file) if trades_file is not None else None
    
    # Optional Day Filter (if the Prices CSV contains multiple days)
    if 'day' in df_prices.columns:
        days = df_prices['day'].unique()
        if len(days) > 1:
            selected_day = st.selectbox("Select Day to View", days)
            df_prices = df_prices[df_prices['day'] == selected_day]

    # Split data into the two specific commodities
    p1 = "ASH_COATED_OSMIUM"
    p2 = "INTARIAN_PEPPER_ROOT"
    
    df_p1 = df_prices[df_prices['product'] == p1].sort_values(by='timestamp')
    df_p2 = df_prices[df_prices['product'] == p2].sort_values(by='timestamp')

    st.divider()

    # Layout Selector
    layout = st.radio(
        "Choose your preferred viewing layout:", 
        ["Stacked Vertically", "Side-by-Side", "Tabs"],
        horizontal=True
    )
    st.write("") # Blank space for visual padding

    # Render based on user's layout choice
    if layout == "Side-by-Side":
        col1, col2 = st.columns(2)
        with col1:
            render_product_dashboard(df_p1, p1, df_trades)
        with col2:
            render_product_dashboard(df_p2, p2, df_trades)
            
    elif layout == "Stacked Vertically":
        render_product_dashboard(df_p1, p1, df_trades)
        st.markdown("<br><br>", unsafe_allow_html=True) # Extra spacing
        render_product_dashboard(df_p2, p2, df_trades)
        
    elif layout == "Tabs":
        tab1, tab2 = st.tabs([p1, p2])
        with tab1:
            render_product_dashboard(df_p1, p1, df_trades)
        with tab2:
            render_product_dashboard(df_p2, p2, df_trades)

else:
    st.info("Please upload your Prices CSV file to begin.")
