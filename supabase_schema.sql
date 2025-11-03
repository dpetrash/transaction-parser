-- Supabase SQL Schema for Transactions Table
-- Run this SQL in your Supabase SQL Editor to create the table

CREATE TABLE IF NOT EXISTS transactions (
    id BIGSERIAL PRIMARY KEY,
    transaction_type VARCHAR(50) NOT NULL,
    name VARCHAR(255),
    email VARCHAR(255),
    amount_usd DECIMAL(10, 2),
    original_amount DECIMAL(10, 2),
    original_currency VARCHAR(10),
    date DATE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for better query performance
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_transactions_email ON transactions(email);
CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions(transaction_type);
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at);

-- Enable Row Level Security (RLS) - adjust policies as needed
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;

-- Create a policy to allow all operations (modify based on your security needs)
-- For development, you might want to allow all operations:
CREATE POLICY "Allow all operations for authenticated users" ON transactions
    FOR ALL
    USING (true)
    WITH CHECK (true);

-- Or for a more secure setup, uncomment and modify:
-- CREATE POLICY "Allow insert for authenticated users" ON transactions
--     FOR INSERT
--     WITH CHECK (true);
-- 
-- CREATE POLICY "Allow select for authenticated users" ON transactions
--     FOR SELECT
--     USING (true);

-- Create a function to update the updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Create a trigger to automatically update updated_at
CREATE TRIGGER update_transactions_updated_at BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

