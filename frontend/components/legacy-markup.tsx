type LegacyMarkupProps = {
  html: string;
};

export function LegacyMarkup({ html }: LegacyMarkupProps) {
  return (
    <div
      data-framework-migration="legacy-markup"
      style={{ display: "contents" }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
