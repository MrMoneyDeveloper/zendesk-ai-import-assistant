export default function Preview({ result }) {
  if (!result) return null;

  return (
    <div style={{ marginTop: "20px" }}>
      <h3>Plan</h3>
      <pre>{JSON.stringify(result.plan, null, 2)}</pre>

      <h3>Generated Data</h3>
      <pre>{JSON.stringify(result.generated_data, null, 2)}</pre>

      <h3>Validation</h3>
      <pre>{JSON.stringify(result.validation, null, 2)}</pre>
    </div>
  );
}