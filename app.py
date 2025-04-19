import streamlit as st
import re
import pandas as pd
from datetime import datetime
import io # Needed for download button

# --- Regular Expressions (same as before) ---
log_line_regex = re.compile(
    r"(\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+" # 1: Timestamp
    r"(Debug|Service|Trade|User_action)\s+"             # 2: Level
    r"'(\d+)':\s+"                                       # 3: Account ID
    r"(.*)"                                              # 4: Message
)
rgx_modify = re.compile(r"modify event #(\d+) (buy|sell) (limit|stop)? ([\d.]+) lots (\S+) at ([\d.]+) tp: ([\d.]+) sl: ([\d.]+)")
rgx_open = re.compile(r"open event #(\d+) (buy|sell) ([\d.]+) lots (\S+) at ([\d.]+)")
rgx_close = re.compile(r"close event #(\d+) (buy|sell) ([\d.]+) lots (\S+) at ([\d.]+) by (\S+)")
rgx_balance_upd = re.compile(r"upd account info balance ([\d.]+)")
rgx_balance_init = re.compile(r"account balance ([\d.]+) USD")
rgx_delete_req = re.compile(r"request delete #(\d+) (.*)")
rgx_delete_ok = re.compile(r"success delete #(\d+) (.*)")
rgx_close_all_req = re.compile(r"request close all orders positions")
rgx_close_all_ok = re.compile(r"success close #(\d+) (.*) at ([\d.]+)")
rgx_close_all_summary = re.compile(r"close (\d+) from (\d+) {.*}")

# --- Modified Parsing Function (takes content string) ---

def generate_trading_journal_from_content(log_content_string):
    """
    Parses trading log content (string) and generates a trading journal DataFrame.

    Args:
        log_content_string (str): The entire content of the log file as a string.

    Returns:
        pd.DataFrame: A Pandas DataFrame representing the trading journal.
                      Returns an empty DataFrame if parsing fails or no data found.
    """
    journal = []
    open_positions = {}
    pending_orders = {}
    last_known_balance = None
    closed_order_ids_pending_pl = []
    account_id = "N/A" # Default account ID

    try:
        log_lines = log_content_string.splitlines()

        for line_num, line in enumerate(log_lines):
            match = log_line_regex.match(line.strip())
            if not match:
                continue

            timestamp_str, level, current_account_id, message = match.groups()
            if account_id == "N/A": # Capture first account ID found
                 account_id = current_account_id

            entry = {
                "Timestamp": timestamp_str,
                "Order/Pos ID": None,
                "Action": None,
                "Direction": None,
                "Type": None,
                "Instrument": None,
                "Volume": None,
                "Price": None,
                "TP": None,
                "SL": None,
                "Notes": "",
                "Balance After Close": None,
                "P/L ($)": None
            }
            processed = False

            # --- Balance Updates ---
            if level == "Service":
                m_bal_init = rgx_balance_init.search(message)
                if m_bal_init:
                    balance = float(m_bal_init.group(1))
                    if last_known_balance is None:
                        last_known_balance = balance
                        # Optionally add a marker for initial balance if desired
                        # entry["Action"] = "Balance Found"
                        # entry["Price"] = balance
                        # journal.append(entry)
                    processed = True

            elif level == "Trade":
                m_bal_upd = rgx_balance_upd.search(message)
                if m_bal_upd:
                    current_balance = float(m_bal_upd.group(1))
                    pl_attributed_in_this_update = False

                    if last_known_balance is not None and closed_order_ids_pending_pl:
                        # Iterate through *pending* P/L assignments
                        temp_closed_ids = list(closed_order_ids_pending_pl) # Copy to iterate while modifying original
                        for closed_id in temp_closed_ids:
                            # Find the journal entry for this closed order
                            for i in range(len(journal) - 1, -1, -1):
                                if journal[i]["Order/Pos ID"] == closed_id and journal[i]["Action"] == "Close" and journal[i]["P/L ($)"] is None:
                                    # Assign P/L based on this specific balance update
                                    trade_pl = current_balance - last_known_balance
                                    journal[i]["Balance After Close"] = current_balance
                                    journal[i]["P/L ($)"] = round(trade_pl, 2)
                                    closed_order_ids_pending_pl.remove(closed_id)
                                    last_known_balance = current_balance # Update balance *after* assigning P/L for this specific close
                                    pl_attributed_in_this_update = True
                                    break # Found and updated the specific close entry
                            if pl_attributed_in_this_update:
                                break # Process only one P/L per balance update for simpler logic

                        # If balance changed but we couldn't attribute it (e.g., multiple closes then one update)
                        if not pl_attributed_in_this_update and closed_order_ids_pending_pl and current_balance != last_known_balance:
                            pl_total = current_balance - last_known_balance
                            st.warning(f"Balance changed by {round(pl_total, 2)} at {timestamp_str}, but could not attribute P/L directly to a single recent close event (IDs: {closed_order_ids_pending_pl}). Manual review might be needed for precise P/L split.")
                            # Clear the queue as we can't accurately assign the P/L split anymore with this simple logic
                            closed_order_ids_pending_pl.clear()
                            last_known_balance = current_balance

                    elif last_known_balance is not None and current_balance != last_known_balance and not closed_order_ids_pending_pl:
                         # Balance changed without a recent known close event
                         pl_total = current_balance - last_known_balance
                         st.info(f"Balance changed by {round(pl_total, 2)} at {timestamp_str} without a directly preceding logged close event (potentially occurred during connection gap or external action).")
                         last_known_balance = current_balance

                    elif last_known_balance is None: # Set initial balance if first seen here
                         last_known_balance = current_balance

                    # Always update the latest known balance for future calcs
                    last_known_balance = current_balance
                    processed = True


            # --- Trade Actions ---
            if level == "Trade" and not processed:
                m_mod = rgx_modify.match(message)
                m_open = rgx_open.match(message)
                m_close = rgx_close.match(message)
                m_close_all_ok = rgx_close_all_ok.match(message)

                if m_mod:
                    order_id, direction, type_suffix, volume, instrument, price, tp, sl = m_mod.groups()
                    entry["Order/Pos ID"] = int(order_id)
                    entry["Action"] = "Place/Mod"
                    entry["Direction"] = direction.capitalize()
                    entry["Type"] = (type_suffix if type_suffix else "Limit/Stop").capitalize()
                    entry["Instrument"] = instrument
                    entry["Volume"] = float(volume)
                    entry["Price"] = float(price)
                    entry["TP"] = float(tp)
                    entry["SL"] = float(sl)
                    pending_orders[entry["Order/Pos ID"]] = entry
                    journal.append(entry)

                elif m_open:
                    order_id, direction, volume, instrument, price = m_open.groups()
                    entry["Order/Pos ID"] = int(order_id)
                    entry["Action"] = "Open"
                    entry["Direction"] = direction.capitalize()
                    entry["Type"] = "Limit Hit?"
                    if entry["Order/Pos ID"] in pending_orders:
                        entry["Type"] = "Limit Hit"
                        entry["TP"] = pending_orders[entry["Order/Pos ID"]].get("TP")
                        entry["SL"] = pending_orders[entry["Order/Pos ID"]].get("SL")
                        del pending_orders[entry["Order/Pos ID"]]
                    else:
                        entry["Type"] = "Market?/Gap?"
                    entry["Instrument"] = instrument
                    entry["Volume"] = float(volume)
                    entry["Price"] = float(price)
                    open_positions[entry["Order/Pos ID"]] = entry
                    journal.append(entry)

                elif m_close:
                    order_id, direction, volume, instrument, price, closed_by = m_close.groups()
                    entry["Order/Pos ID"] = int(order_id)
                    entry["Action"] = "Close"
                    entry["Direction"] = direction.capitalize()
                    entry["Price"] = float(price) # Entry price recorded in log
                    entry["Instrument"] = instrument
                    entry["Volume"] = float(volume)
                    entry["Notes"] = f"Closed by {closed_by}"
                    if entry["Order/Pos ID"] in open_positions:
                        del open_positions[entry["Order/Pos ID"]]
                    closed_order_ids_pending_pl.append(entry["Order/Pos ID"]) # Mark for P/L calc
                    journal.append(entry)

                elif m_close_all_ok:
                     order_id, details, close_price = m_close_all_ok.groups()
                     entry["Order/Pos ID"] = int(order_id)
                     entry["Action"] = "Close OK"
                     entry["Notes"] = f"Part of Close All. Confirmed @ {close_price}"
                     # Update note if Close event already exists
                     found = False
                     for i in range(len(journal) - 1, -1, -1):
                         if journal[i]["Order/Pos ID"] == entry["Order/Pos ID"] and journal[i]["Action"] == "Close":
                             journal[i]["Notes"] += f". Close OK @ {close_price}"
                             found = True
                             break
                     if not found: journal.append(entry)

            # --- User Actions ---
            elif level == "User_action" and not processed:
                m_del_req = rgx_delete_req.match(message)
                m_del_ok = rgx_delete_ok.match(message)
                m_close_all_req = rgx_close_all_req.match(message)

                if m_del_req:
                    order_id, details = m_del_req.groups()
                    entry["Order/Pos ID"] = int(order_id)
                    entry["Action"] = "Delete Req"
                    entry["Notes"] = f"User: {details}"
                    journal.append(entry)
                elif m_del_ok:
                    order_id, details = m_del_ok.groups()
                    entry["Order/Pos ID"] = int(order_id)
                    entry["Action"] = "Delete OK"
                    entry["Notes"] = f"Success: {details}"
                    if entry["Order/Pos ID"] in pending_orders:
                         del pending_orders[entry["Order/Pos ID"]]
                    journal.append(entry)
                elif m_close_all_req:
                    entry["Action"] = "Close All Req"
                    entry["Notes"] = "User requested close all"
                    journal.append(entry)


    except Exception as e:
        st.error(f"An error occurred during log processing: {e}")
        return pd.DataFrame() # Return empty DataFrame on error

    # --- Convert to DataFrame and Finalize ---
    if not journal:
        return pd.DataFrame() # Return empty if no relevant entries found

    df = pd.DataFrame(journal)

    # Define standard column order
    cols = ["Timestamp", "Order/Pos ID", "Action", "Direction", "Type",
            "Instrument", "Volume", "Price", "TP", "SL", "Notes",
            "Balance After Close", "P/L ($)"]
    # Add missing columns if they weren't created
    for col in cols:
        if col not in df.columns:
            df[col] = None # Use None for missing values
    df = df[cols] # Reorder

    # Convert relevant columns to numeric, coercing errors
    num_cols = ["Order/Pos ID", "Volume", "Price", "TP", "SL", "Balance After Close", "P/L ($)"]
    for col in num_cols:
         if col in df.columns:
             df[col] = pd.to_numeric(df[col], errors='coerce') # Coerce turns errors into NaT/NaN

    # Fill NaN values in display columns for better presentation (optional)
    # df.fillna('', inplace=True) # Or fill specific columns

    return df

# --- Streamlit App Interface ---

st.set_page_config(layout="wide") # Use wider layout
st.title("📈 Trading Log Journal Generator")
st.markdown("""
Upload your trading log file (`.txt` format) to automatically generate a trading journal.

**Features:**
*   Parses trade open, close, modify, and delete events.
*   Tracks pending and open orders.
*   Attempts to calculate Profit/Loss (P/L) based on balance updates following close events.
*   Displays the journal in a sortable table.
*   Allows downloading the journal as a CSV file.

**Note:** P/L calculation accuracy depends on the timing of balance updates in the log relative to close events. Multiple rapid closes before a single balance update might not be split accurately by this simple parser. Check warnings for potential attribution issues.
""")

uploaded_file = st.file_uploader("Choose a trading log file (.txt)", type="txt")

if uploaded_file is not None:
    # Read content
    try:
        log_content = uploaded_file.getvalue().decode("utf-8")
        st.info(f"Processing uploaded file: {uploaded_file.name}...")

        # Generate journal
        journal_df = generate_trading_journal_from_content(log_content)

        if not journal_df.empty:
            st.success("Journal generated successfully!")
            st.subheader("Generated Trading Journal")

            # Display DataFrame
            st.dataframe(journal_df)

            # --- Download Button ---
            # Convert DataFrame to CSV in memory
            csv_buffer = io.StringIO()
            journal_df.to_csv(csv_buffer, index=False, encoding='utf-8')
            csv_data = csv_buffer.getvalue().encode('utf-8')

            # Create filename
            base_filename = uploaded_file.name.rsplit('.', 1)[0] # Remove .txt extension
            download_filename = f"journal_{base_filename}.csv"

            st.download_button(
                label="⬇️ Download Journal as CSV",
                data=csv_data,
                file_name=download_filename,
                mime='text/csv',
            )
        else:
            st.warning("No relevant trading activity found or parsed from the log file.")

    except UnicodeDecodeError:
         st.error("Error decoding file. Please ensure the log file is UTF-8 encoded.")
    except Exception as e:
        st.error(f"An unexpected error occurred: {e}")

else:
    st.info("☝️ Upload a log file to begin.")

st.markdown("---")
st.caption("App created based on provided log format.")
