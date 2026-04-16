import streamlit as st
import pandas as pd
import plotly.graph_objects as go

# 1. Page Configuration (Keep it wide to fit two charts)
st.set_page_config(page_title="IMC Prosperity Dual Viewer", layout="wide", page_icon="📈")

@st.cache_data
def load_data(file):
    # Load data and cache it so it doesn't reload on every UI click
    df = pd.read_csv(file, sep=';')
    return df

def render_product_dashboard(product_df, product_name):
    """Helper function to render metrics and a chart for a specific product."""
    if product_df.empty:
        st.warning(f"No data found for {product_name} in this file.")
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
    
    with st.expander(f"View Raw Data: {product_name}"):
        st.dataframe(product_df, use_container_width=True)


# Main App Execution
st.title("📈 IMC Prosperity: Dual Commodity Viewer")
st.markdown("Upload your CSV file to simultaneously view **ASH_COATED_OSMIUM** and **INTARIAN_PEPPER_ROOT**.")

uploaded_file = st.file_uploader("Upload Prices CSV", type=["csv"])

if uploaded_file is not None:
    df = load_data(uploaded_file)
    
    # Optional Day Filter (if the CSV contains multiple days)
    days = df['day'].unique()
    if len(days) > 1:
        selected_day = st.selectbox("Select Day to View", days)
        df = df[df['day'] == selected_day]

    # Split data into the two specific commodities
    p1 = "ASH_COATED_OSMIUM"
    p2 = "INTARIAN_PEPPER_ROOT"
    
    df_p1 = df[df['product'] == p1].sort_values(by='timestamp')
    df_p2 = df[df['product'] == p2].sort_values(by='timestamp')

    st.divider()

    # Layout Selector
    layout = st.radio(
        "Choose your preferred viewing layout:", 
        ["Side-by-Side", "Stacked Vertically", "Tabs"],
        horizontal=True
    )
    st.write("") # Blank space for visual padding

    # Render based on user's layout choice
    if layout == "Side-by-Side":
        col1, col2 = st.columns(2)
        with col1:
            render_product_dashboard(df_p1, p1)
        with col2:
            render_product_dashboard(df_p2, p2)
            
    elif layout == "Stacked Vertically":
        render_product_dashboard(df_p1, p1)
        st.markdown("<br><br>", unsafe_allow_html=True) # Extra spacing
        render_product_dashboard(df_p2, p2)
        
    elif layout == "Tabs":
        tab1, tab2 = st.tabs([p1, p2])
        with tab1:
            render_product_dashboard(df_p1, p1)
        with tab2:
            render_product_dashboard(df_p2, p2)

else:
    st.info("Please upload a CSV file to begin.")
